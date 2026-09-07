# Automatic-review boundary

Automatic review should operate as a companion tool through Post Office facilities.

| Model | Assessment |
|---|---|
| Independent service | Reject. It duplicates exact-byte custody, Drive/browser transport, reviewer queues, monitoring, recovery, and evidence retention. |
| Companion through Post Office | Adopt. Every project gets a review-specific interface while one trusted transport implementation retains custody and queue truth. |
| Post Office function | Reject as the caller boundary. It makes review depend on postal identities and couples review evolution to mail semantics. |

The companion's caller credential is task/host bound and permits only review submission, status,
and completion. It may use the courier's active mailbox as the internal credential subject, so the
requesting task does not need a postal mailbox and receives no postal authority. Coordinator and
browser operations remain courier-only. Google Drive remains a transient browser bridge and never
becomes retained review storage.

The companion source lives in `plugins/automatic-code-review`. Post Office Next will eventually
serve the same client contract through an authenticated local broker. Until the separately
authorised switchover, the companion's legacy adapter may call the current read-only review service.
