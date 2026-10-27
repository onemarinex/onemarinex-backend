# Crew manifest PDFs leave this system

A crew list is the most sensitive file the platform accepts. One IMO Crew List
carries, for every person aboard: full name, nationality, rank, and **passport
number**.

CSV and Excel manifests are parsed here and never leave. **PDF manifests are
not.** The whole document is base64-encoded and sent to Anthropic's API, where
a model reads the printed form and returns the rows. That is the only way the
platform supports PDF today — the IMO form is laid out visually, so there are
no columns to match.

The read-only audit of 25 October 2026 raised this as a processor boundary
"whose configuration, retention and approved data handling were not verified",
and asked that format support be preserved while accounting for it. This
document is that accounting.

## What is sent

| | |
|---|---|
| **Sent** | The complete PDF, exactly as uploaded, plus a fixed extraction prompt |
| **Not sent** | Anything else. No vessel record, no agency, no user, no database content |
| **Where** | Anthropic Messages API, model `CREW_MANIFEST_MODEL` (default `claude-haiku-4-5`) |
| **Credential** | `ANTHROPIC_API_KEY` |
| **Returned** | Name, rank, nationality and passport number per row — parsed, shown to the agent for review, and only persisted after they confirm |

Nothing is cached on our side: the bytes are read from the upload, sent, and
dropped when the request ends.

## Turning it off

```
MANIFEST_PDF_PROCESSOR=off
```

PDF uploads are then refused with a message telling the agent to upload CSV or
Excel instead. Those formats are read locally and are unaffected. The default
is `anthropic`, which preserves existing behaviour — the switch exists so an
operator can decline the boundary without a code change, not so that anyone is
opted out by surprise.

## The record of each crossing

Every PDF that goes out writes one log line:

```
manifest.pdf.external_processing file=<name> bytes=<n> model=<model> rows=<n>
```

File name, size, model and how many rows came back. **No manifest content** —
no name, nationality or passport number appears in a log. It is enough to
answer "which manifests were processed externally, and when", which is the
question a data-protection review asks.

A log line is not an audit table. If the processor boundary has to be
demonstrable rather than merely traceable — a retained record per manifest,
queryable, with the uploading agent and vessel — that is a schema change and
an owner decision, and it is not built.

## What this document does not settle

Three things are agreement, not code, and none of them can be answered from
this repository:

1. **Retention** — how long the processor holds the document.
2. **Training use** — whether the content may be used to train models.
3. **Sub-processors** — who else sees it downstream.

Those belong in the processor agreement. Until they are confirmed, the honest
position is: a PDF crew list is disclosed to a third party, the operator can
turn that off, and CSV or Excel avoids it entirely.
