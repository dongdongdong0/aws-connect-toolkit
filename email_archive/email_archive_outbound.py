"""
Archive an outgoing agent email from Amazon Connect to an external mailbox.

Runs on the OUTBOUND email flow, from an Invoke AWS Lambda block.

Why this exists
    Connect sends and receives through SES, and this reply leaves Connect
    through SES straight to the customer - it never passes back through the
    organisation's own mail server, so a copy kept there would miss it
    entirely. This is the companion to the inbound archive Lambda.

Two cases
    Agent replied to a customer email
        The flow event carries RelatedContactId, pointing at the inbound
        contact. Threading hangs off that: In-Reply-To is the customer's
        Message-ID, and the subject gets a Re: prefix if it has none.

    Agent started the conversation (New email in the workspace)
        There is no related contact, so this copy is the root of its own
        thread: no In-Reply-To, no Re: prefix. Address and subject come
        straight off the event, with no extra API call.

Threading
    SES rewrites Message-ID on send but passes References through untouched, and
    Connect exposes only each email's own Message-ID (connect:EmailMessageId),
    never the References chain. So both Lambdas put a shared synthetic anchor in
    References:

        anchor = sha1(customer address + subject, Re:/Fw: prefixes stripped)

    That is also why an agent-initiated email threads correctly once the
    customer replies: the inbound Lambda hashes the same two values, their
    Re: prefix stripped before hashing.

    Two consequences, by design: two unrelated conversations from the same
    address with the same subject merge into one thread, and a customer who
    edits the subject mid-thread starts a new one. Only the primary recipient
    (CustomerEndpoint) feeds the anchor, so a reply from a Cc recipient starts
    its own thread.

Lambda configuration
    Runtime   Python 3.12 or later
    Timeout   60s   - the 3s default dies while downloading attachments
    Memory    512MB - 128MB runs out base64-encoding a large attachment

Environment variables
    INSTANCE_ID     - Amazon Connect instance ID (required)
    AWS_REGION_NAME - optional override; Lambda's own AWS_REGION is used otherwise
    LOG_PAYLOADS    - set to 1 to log full API payloads while debugging. Off by
                      default: those payloads contain customer addresses, names,
                      subjects and every contact attribute.

Parameters from the Invoke AWS Lambda block (event.Details.Parameters)
    archive_email   - destination mailbox for the archive copy
    from_email      - sender address; must be a verified SES identity in this
                      region. A Connect-generated *.email.connect.aws address is
                      not an SES identity and will be rejected.

Execution mode
    Set the Invoke AWS Lambda block to ASYNCHRONOUS. Synchronous caps at 8
    seconds, which this function can exceed with attachments, and Connect
    retries a failed synchronous invoke, which can send duplicate copies.

IAM policy for the execution role
    logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
        on the function's own log group (both the bare ARN and the :* form -
        CreateLogGroup matches the first, the other two match the second)
    connect:DescribeContact, connect:ListContactReferences,
    connect:GetAttachedFile, connect:BatchGetAttachedFileMetadata
        on arn:aws:connect:<region>:<account>:instance/*
    ses:SendEmail, ses:SendRawEmail
        SESv2 send_email with Raw content needs SendRawEmail as well as SendEmail

Notes from building this
    - SES overwrites the raw message's From header with whatever
      FromEmailAddress says, so a display name set only in the MIME is lost on
      send. To use one, pass the same formatted string to both.
    - Connect flattens the customer's MIME tree into separate stored files, so
      the link between an <img src="cid:..."> and its image is lost. The inline
      image handling below reconstructs it by filename, then by position.
    - Connect types inbound attachments as application/octet-stream, so the
      content type is re-derived from the file extension.
"""


import os
import re
import json
import base64
import hashlib
import mimetypes
import logging
import urllib.request

from html import escape, unescape
from email.message import EmailMessage
from email.utils import make_msgid, formatdate

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Lambda sets AWS_REGION itself. AWS_REGION_NAME is only an override, because
# AWS_REGION is reserved and cannot be set as a custom variable.
REGION = os.environ.get("AWS_REGION_NAME") or os.environ["AWS_REGION"]
INSTANCE_ID = os.environ["INSTANCE_ID"]

connect = boto3.client("connect", region_name=REGION)
ses = boto3.client("sesv2", region_name=REGION)

# Full event, contact and reference dumps carry customer addresses, names,
# subject lines and every contact attribute into CloudWatch, where they then
# sit for the log group's retention period. They are off by default; set
# LOG_PAYLOADS=1 on the function while debugging a specific contact, and turn
# it off again afterwards.
#
# Attachment file names are still logged either way: they are needed to debug
# the cid-to-attachment matching below, and a name can itself be sensitive.
# Decide whether that is acceptable for your deployment.
LOG_PAYLOADS = os.environ.get("LOG_PAYLOADS", "").lower() in ("1", "true", "yes")


def mask_address(address):
    """j***@example.com - enough to debug routing without storing the address."""
    if LOG_PAYLOADS:
        return address
    if not address or "@" not in address:
        return "<address>"
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


def log_payload(label, payload):
    """Dump a full API payload, only when LOG_PAYLOADS is on."""
    if LOG_PAYLOADS:
        logger.info("%s: %s", label, json.dumps(payload, default=str))



def lambda_handler(event, context):
    log_payload("Event", event)

    contact_data = event["Details"]["ContactData"]
    attributes = contact_data.get("Attributes", {})
    parameters = event["Details"].get("Parameters", {})

    outbound_contact_id = contact_data["ContactId"]

    # The flow event carries RelatedContactId on an AGENT_REPLY contact, and it
    # points at the inbound email this reply answers. Fall back to the
    # inbound_contact_id attribute for flows that still set it.
    inbound_contact_id = contact_data.get("RelatedContactId") or attributes.get(
        "inbound_contact_id"
    )
    subject = attributes.get("email_subject", "(no subject)")

    archive_email = parameters.get("archive_email")
    from_email = parameters.get("from_email")

    if not archive_email or not from_email:
        logger.error(
            "Missing required parameters: archive_email=%s, from_email=%s",
            archive_email,
            from_email,
        )
        return {"status": "error", "reason": "missing archive_email or from_email parameter"}

    if inbound_contact_id:
        # Agent replied to a customer email. Threading hangs off that inbound
        # contact, and the anchor is computed from it so the inbound Lambda
        # produces the identical value for the same conversation.
        is_reply = True
        logger.info("Resolved inbound contact: %s", inbound_contact_id)

        inbound_contact = describe_contact(inbound_contact_id)
        threading_headers = extract_threading_headers(inbound_contact)

        customer_address = inbound_contact.get("CustomerEndpoint", {}).get("Address")
        anchor_subject = (
            inbound_contact.get("SegmentAttributes", {})
            .get("connect:EmailSubject", {})
            .get("ValueString")
        )

        # Fallback subject from inbound if we don't have one
        if (subject == "(no subject)" or not subject) and anchor_subject:
            subject = (
                f"Re: {anchor_subject}"
                if not anchor_subject.lower().startswith("re:")
                else anchor_subject
            )
    else:
        # Agent started the conversation (New email in the workspace). There is
        # no inbound contact, so this copy is the root of its own thread: no
        # In-Reply-To, and no "Re:" on the subject.
        #
        # The anchor still comes out right. It hashes customer address plus
        # normalized subject, and when the customer replies the inbound Lambda
        # hashes the same two values — their "Re:" prefix is stripped before
        # hashing — so that reply lands in this thread.
        #
        # Everything needed is already on the event; no extra API call.
        is_reply = False
        logger.info("No related inbound contact; archiving as agent-initiated email")

        threading_headers = {"message_id": None, "references": None, "in_reply_to": None}

        customer_address = contact_data.get("CustomerEndpoint", {}).get("Address")
        anchor_subject = (
            contact_data.get("SegmentAttributes", {})
            .get("connect:EmailSubject", {})
            .get("ValueString")
        )

        if (subject == "(no subject)" or not subject) and anchor_subject:
            subject = anchor_subject

    anchor = thread_anchor_id(
        customer_address=customer_address,
        subject=anchor_subject,
        from_email=from_email,
    )

    # 2. Pull HTML body, plain text body and attachments from outbound contact
    html_body, plain_body, attachments = get_outbound_content(outbound_contact_id)


    # 3. Build MIME and send
    raw_bytes = build_mime(
        subject=subject,
        html_body=html_body,
        plain_body=plain_body,
        attachments=attachments,
        threading_headers=threading_headers,
        from_email=from_email,
        archive_email=archive_email,
        customer_address=customer_address,
        is_reply=is_reply,
        anchor=anchor,
    )

    response = ses.send_email(
        FromEmailAddress=from_email,
        Destination={"ToAddresses": [archive_email]},
        Content={"Raw": {"Data": raw_bytes}},
    )

    logger.info("Sent archive, SES message id: %s", response.get("MessageId"))
    return {"status": "ok", "ses_message_id": response.get("MessageId")}


SUBJECT_PREFIX_RE = re.compile(r"^(?:\s*(?:re|fw|fwd|aw|sv|vs|rv)\s*:\s*)+", re.I)


def thread_anchor_id(customer_address, subject, from_email):
    """
    Build a synthetic, stable Message-ID that every archive copy in the same
    conversation will carry in its References header.

    Connect only exposes each email's own Message-ID (connect:EmailMessageId),
    not the References chain the customer's mail client sent. Without a shared
    anchor, round 2 of a conversation would start a new thread in the archive
    mailbox. Hashing customer address + normalized subject gives both Lambdas
    the same value without any extra API calls.
    """
    normalized_subject = SUBJECT_PREFIX_RE.sub("", subject or "").strip().lower()
    key = f"{(customer_address or '').strip().lower()}|{normalized_subject}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    domain = (from_email or "archive.local").split("@")[-1]
    anchor = f"<connect-thread-{digest}@{domain}>"
    if LOG_PAYLOADS:
        logger.info("Thread anchor key=%r -> %s", key, anchor)
    else:
        # The anchor is already a digest, and it is the value that has to match
        # between the two Lambdas, so it is the useful half to log.
        logger.info("Thread anchor -> %s", anchor)
    return anchor



def describe_contact(contact_id):
    """Call DescribeContact and log the full response for inspection."""
    try:
        resp = connect.describe_contact(
            InstanceId=INSTANCE_ID,
            ContactId=contact_id,
        )
        log_payload(f"DescribeContact({contact_id})", resp)
        return resp.get("Contact", {})
    except Exception as e:
        logger.exception("DescribeContact failed for %s: %s", contact_id, e)
        return {}


def extract_threading_headers(contact):
    """
    Pull the inbound email's Message-ID from Contact.SegmentAttributes.
    """
    segment_attrs = contact.get("SegmentAttributes", {})
    message_id = segment_attrs.get("connect:EmailMessageId", {}).get("ValueString")

    return {
        "message_id": message_id,
        "references": None,
        "in_reply_to": None,
    }


def get_outbound_content(contact_id):
    """
    From the outbound contact, pull:
      - EMAIL_MESSAGE reference            (HTML body, inline images included)
      - EMAIL_MESSAGE_PLAIN_TEXT reference (the agent's text, as Connect stored it)
      - ATTACHMENT references              (real attached files via "Attach file")
    Returns (html_body or None, plain_body or None, attachments list).
    """
    html_body = None
    plain_body = None
    attachments = []

    try:
        resp = connect.list_contact_references(
            InstanceId=INSTANCE_ID,
            ContactId=contact_id,
            ReferenceTypes=["ATTACHMENT", "EMAIL_MESSAGE", "EMAIL_MESSAGE_PLAIN_TEXT"],
        )
        log_payload(f"ListContactReferences({contact_id})", resp)

        ref_list = resp.get("ReferenceSummaryList", [])

        for ref in ref_list:
            email_msg = ref.get("EmailMessage")
            if email_msg:
                html_body = download_message_body(email_msg, contact_id, "EmailMessage")
                continue

            plain_msg = ref.get("EmailMessagePlainText")
            if plain_msg:
                plain_body = download_message_body(
                    plain_msg, contact_id, "EmailMessagePlainText"
                )
                continue

            att = ref.get("Attachment")
            if att:
                attachment = download_attachment(att, contact_id)
                if attachment:
                    attachments.append(attachment)

    except Exception as e:
        logger.exception("ListContactReferences failed: %s", e)

    logger.info(
        "Outbound content: html_body=%s, plain_body=%s, attachments=%d",
        "present" if html_body else "none",
        "present" if plain_body else "none",
        len(attachments),
    )
    return html_body, plain_body, attachments


def build_contact_arn(file_arn, contact_id):
    account_id = file_arn.split(":")[4]
    return (
        f"arn:aws:connect:{REGION}:{account_id}:"
        f"instance/{INSTANCE_ID}/contact/{contact_id}"
    )


def download_message_body(ref, contact_id, label):
    """
    Download an EMAIL_MESSAGE or EMAIL_MESSAGE_PLAIN_TEXT reference and return
    its body. Connect stores both as JSON:
        {"contentType": "text/html", "messageContent": "..."}
    A non-JSON file is treated as the body itself.
    """
    try:
        arn = ref.get("Arn")
        if not arn or "/file/" not in arn:
            logger.warning("%s ref missing file ARN: %s", label, ref)
            return None

        file_id = arn.split("/file/")[-1]
        file_resp = connect.get_attached_file(
            InstanceId=INSTANCE_ID,
            FileId=file_id,
            AssociatedResourceArn=build_contact_arn(arn, contact_id),
        )
        download_url = file_resp["DownloadUrlMetadata"]["Url"]

        with urllib.request.urlopen(download_url) as r:
            raw = r.read()

        try:
            payload = json.loads(raw)
            body = payload.get("messageContent")
            content_type = payload.get("contentType")
        except (ValueError, AttributeError):
            body = raw.decode("utf-8", "replace")
            content_type = "(not JSON)"

        logger.info(
            "%s downloaded: contentType=%s, bodyLen=%d",
            label,
            content_type,
            len(body or ""),
        )
        return body
    except Exception as e:
        logger.exception("Failed to download %s: %s", label, e)
        return None


def download_attachment(att_ref, contact_id):
    """Download one attached file via GetAttachedFile pre-signed URL."""
    try:
        arn = att_ref.get("Arn")
        name = att_ref.get("Name", "attachment")

        if not arn or "/file/" not in arn:
            logger.warning("Attachment missing file ARN: %s", att_ref)
            return None

        file_id = arn.split("/file/")[-1]
        account_id = arn.split(":")[4]
        contact_arn = (
            f"arn:aws:connect:{REGION}:{account_id}:"
            f"instance/{INSTANCE_ID}/contact/{contact_id}"
        )

        file_resp = connect.get_attached_file(
            InstanceId=INSTANCE_ID,
            FileId=file_id,
            AssociatedResourceArn=contact_arn,
        )
        filename = file_resp.get("FileName") or name or f"attachment-{file_id}"
        download_url = file_resp["DownloadUrlMetadata"]["Url"]

        with urllib.request.urlopen(download_url) as r:
            data = r.read()
            content_type = r.headers.get("Content-Type", "application/octet-stream")

        content_type = resolve_content_type(filename, content_type)

        logger.info(
            "Attachment downloaded: name=%s, size=%d, type=%s",
            filename,
            len(data),
            content_type,
        )

        return {
            "filename": filename,
            "content_type": content_type,
            "data": data,
        }
    except Exception as e:
        logger.exception("Failed to download attachment: %s", e)
        return None


# ---------------------------------------------------------------------------
# Inline images
# ---------------------------------------------------------------------------
# Connect hands us inline images in two different shapes:
#
#   inbound customer email - the HTML keeps the sender's <img src="cid:...">
#                            and the image arrives as a separate ATTACHMENT,
#                            typed application/octet-stream with the sender's
#                            original filename, which rarely matches the cid
#   agent reply            - the workspace editor embeds the image into the
#                            HTML as a data: URI
#
# A cid: reference with no matching MIME part is just a broken image, so both
# shapes get turned into real multipart/related parts carrying a Content-ID.

CID_SRC_RE = re.compile(r'src\s*=\s*["\']cid:([^"\']+)["\']', re.I)
DATA_URI_RE = re.compile(
    r'src\s*=\s*["\']data:(image/[a-z0-9.+\-]+);base64,([A-Za-z0-9+/=\s]+?)["\']',
    re.I,
)
IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|bmp|webp|tiff?|svg|ico|heic)$", re.I)

GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream"}

# Some clients render a data: URI in <img src>, others drop it. Outlook desktop
# is the strict one, and an archive mailbox is usually Outlook, so cid: is the
# safer form to send. Set this to False to pass data: URIs through untouched.
CONVERT_DATA_URIS = True

# SES caps a message at 40 MB including attachments, measured on the encoded
# MIME. Leave headroom so one huge file degrades the archive copy instead of
# failing the send outright.
MAX_TOTAL_BYTES = 35 * 1024 * 1024


def split_content_type(content_type):
    maintype, _, subtype = (content_type or "").partition("/")
    if not maintype or not subtype:
        return "application", "octet-stream"
    return maintype, subtype


def resolve_content_type(filename, declared):
    """
    Connect types inbound attachments as application/octet-stream, which stops
    mail clients from rendering an inline image. Fall back to the filename
    extension when the declared type is missing or generic.
    """
    if (declared or "").lower() not in GENERIC_TYPES:
        return declared
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed:
        logger.info("Retyped %s: %s -> %s", filename, declared, guessed)
        return guessed
    return declared or "application/octet-stream"


def looks_like_image(att):
    if (att.get("content_type") or "").lower().startswith("image/"):
        return True
    return bool(IMAGE_EXT_RE.search(att.get("filename") or ""))


SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
LINE_BREAK_RE = re.compile(r"<(?:br|/p|/div|/tr|/h[1-6])\b[^>]*>", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(html_body, limit=20000):
    """
    Last-resort plain text when Connect has no EMAIL_MESSAGE_PLAIN_TEXT
    reference. Must run on the HTML *after* inline images have been rewritten
    to cid:, otherwise the base64 of a data: URI ends up in the text body.
    """
    if not html_body:
        return ""
    text = SCRIPT_STYLE_RE.sub(" ", html_body)
    text = LINE_BREAK_RE.sub("\n", text)
    text = TAG_RE.sub("", text)
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]*\n+", "\n\n", text).strip()
    return text[:limit]


def prepare_inline_images(html_body, attachments):
    """
    Turn both shapes of inline image into cid: references backed by a real
    MIME part. Returns (html_body, inline_images, remaining_attachments).
    """
    if not html_body:
        return html_body, [], attachments

    inline = []
    counter = [0]

    def replace_data_uri(m):
        content_type = m.group(1).lower()
        b64 = re.sub(r"\s+", "", m.group(2))
        try:
            data = base64.b64decode(b64)
        except Exception:
            logger.warning("Could not decode a data: URI, leaving it in place")
            return m.group(0)
        counter[0] += 1
        ext = content_type.split("/")[-1].split("+")[0]
        cid = f"connect-inline-{counter[0]}@archive"
        inline.append(
            {
                "cid": cid,
                "filename": f"inline-{counter[0]}.{ext}",
                "content_type": content_type,
                "data": data,
            }
        )
        return f'src="cid:{cid}"'

    n_data = 0
    if CONVERT_DATA_URIS:
        html_body, n_data = DATA_URI_RE.subn(replace_data_uri, html_body)

    # Any cid: reference still in the HTML came from the sender's own client,
    # so find the attachment it points at.
    cid_refs = CID_SRC_RE.findall(html_body)
    own = {i["cid"] for i in inline}
    unresolved = [c for c in cid_refs if c not in own]
    logger.info("cid refs in HTML: %s", cid_refs)

    used = set()
    matched = {}

    # Pass 1: match on filename. Outlook names its cids "image001.png@01DC...",
    # i.e. the filename sits before the '@'.
    for cid in unresolved:
        local = cid.split("@")[0].lower()
        stem = local.rsplit(".", 1)[0]
        for att in attachments:
            if id(att) in used:
                continue
            name = (att.get("filename") or "").lower()
            if name == local or name.rsplit(".", 1)[0] == stem:
                matched[cid] = att
                used.add(id(att))
                logger.info("cid %s matched attachment %r by name", cid, name)
                break

    # Pass 2: Connect keeps the sender's original filename, which usually has
    # nothing to do with the cid, so pair what is left in document order. Only
    # safe while the counts agree.
    leftover_cids = [c for c in unresolved if c not in matched]
    leftover_images = [
        a for a in attachments if id(a) not in used and looks_like_image(a)
    ]
    if leftover_cids and len(leftover_cids) == len(leftover_images):
        for cid, att in zip(leftover_cids, leftover_images):
            matched[cid] = att
            used.add(id(att))
            logger.info(
                "cid %s paired with attachment %r by position", cid, att["filename"]
            )

    for cid, att in matched.items():
        inline.append(
            {
                "cid": cid,
                "filename": att["filename"],
                "content_type": att["content_type"],
                "data": att["data"],
            }
        )

    remaining = [a for a in attachments if id(a) not in used]

    logger.info(
        "Inline images: %d from data: URIs, %d cid refs in HTML "
        "(%d resolved), %d regular attachments left",
        n_data,
        len(cid_refs),
        len(matched),
        len(remaining),
    )
    still_broken = [c for c in unresolved if c not in matched]
    if still_broken:
        logger.warning("cid refs with no matching attachment: %s", still_broken)

    return html_body, inline, remaining


BODY_NOTE_STYLE = (
    "font:12px -apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
    "color:#5f6368;margin:0 0 12px 0;padding:0 0 8px 0;"
    "border-bottom:1px solid #dadce0;"
)
BODY_TAG_RE = re.compile(r"(<body\b[^>]*>)", re.I)


def prepend_note(html_body, plain_body, note):
    """
    Put one line at the top of both bodies saying which way this copy went and
    who the customer is.

    Every archive copy leaves from the same verified SES identity, so the From
    header is identical on all of them and the mailbox cannot tell inbound from
    outbound on its own. Keep the line short: mail clients build their preview
    text from the top of the plain part, so a long block would push the real
    content out of the list view.
    """
    if html_body:
        block = f'<div style="{BODY_NOTE_STYLE}">{escape(note)}</div>\n'
        if BODY_TAG_RE.search(html_body):
            html_body = BODY_TAG_RE.sub(
                lambda m: m.group(1) + block, html_body, count=1
            )
        else:
            html_body = block + html_body

    plain_body = f"{note}\n\n{plain_body or ''}".rstrip() + "\n"
    return html_body, plain_body


def build_mime(
    subject,
    html_body,
    plain_body,
    attachments,
    threading_headers,
    from_email,
    archive_email,
    customer_address,
    is_reply,
    anchor,
):
    html_body, inline_images, attachments = prepare_inline_images(
        html_body, attachments
    )

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = archive_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)

    # SES overwrites Message-ID on send, so threading has to ride on References,
    # which SES passes through untouched.
    msg["Message-ID"] = make_msgid()

    inbound_msg_id = threading_headers.get("message_id")

    if inbound_msg_id:
        msg["In-Reply-To"] = inbound_msg_id
        msg["References"] = f"{anchor} {inbound_msg_id}"
    else:
        msg["References"] = anchor

    plain_body = plain_body or html_to_text(html_body) or "(no text content)"

    label = "Agent reply" if is_reply else "Agent email"
    note = f"{label} to {customer_address}" if customer_address else label
    html_body, plain_body = prepend_note(html_body, plain_body, note)

    # Prefer HTML body (contains inline images); fall back to plain text
    if html_body:
        # Set plain text version as the main content, then add HTML alternative
        msg.set_content(plain_body)
        msg.add_alternative(html_body, subtype="html")

        # Hang the inline images off the HTML part so it becomes
        # multipart/related and the cid: references resolve.
        if inline_images:
            html_part = msg.get_payload()[-1]
            for img in inline_images:
                maintype, subtype = split_content_type(img["content_type"])
                html_part.add_related(
                    img["data"],
                    maintype=maintype,
                    subtype=subtype,
                    cid=f"<{img['cid']}>",
                    filename=img["filename"],
                    disposition="inline",
                )
    else:
        msg.set_content(plain_body or "")

    budget = MAX_TOTAL_BYTES - len(msg.as_bytes())
    skipped = []

    for att in attachments:
        encoded_size = (len(att["data"]) * 4) // 3 + 1024
        if encoded_size > budget:
            skipped.append((att["filename"], len(att["data"])))
            continue
        budget -= encoded_size
        maintype, subtype = split_content_type(att["content_type"])
        msg.add_attachment(
            att["data"],
            maintype=maintype,
            subtype=subtype,
            filename=att["filename"],
        )

    if skipped:
        logger.warning(
            "Skipped attachments that would exceed the SES message size limit: %s",
            skipped,
        )
        note = "\n".join(
            f"[not archived - too large for email: {name} ({size} bytes)]"
            for name, size in skipped
        )
        msg.add_attachment(
            note.encode("utf-8"),
            maintype="text",
            subtype="plain",
            filename="SKIPPED-ATTACHMENTS.txt",
        )

    logger.info(
        "Archive headers: From=%s To=%s",
        mask_address(msg["From"]),
        mask_address(msg["To"]),
    )
    return msg.as_bytes()