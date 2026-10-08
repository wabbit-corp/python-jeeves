# Vox Discord intent review — prepared form

Application **1382045038501953729**. Prepared October 8, 2026. The form is filled in Chrome; acknowledgement is unchecked and Submit remains untouched.

[Review the public evidence gallery](https://vox.wabbit.one/evidence/2026-10-08/). Eight full-resolution screenshots blur names, avatars, inline references, and the complete unrelated Direct Messages list. Other decoded pixels are unchanged.

## Selections

| Field | Prepared answer |
| --- | --- |
| Server Members Intent | Unchecked |
| Presence Intent | Unchecked |
| Message Content Intent | Checked |
| Public privacy policy | Yes |
| Users can opt out | Yes |
| Stored outside Discord | Yes |
| Stored for 30 days or less | No |
| Encrypted at rest | Yes |
| Used to train ML/AI models | No |

## What does your application do?

Vox is a conversational assistant for Discord communities. Members ask questions, summarize discussions, find relevant earlier conversations, create reminders, and track commitments. Vox uses surrounding messages from multiple participants to understand references to an earlier question or discussion, so members do not need to copy that conversation into their request. Members can address it through @Vox, DMs, or the conversational names "v" and "vox".

Members can also choose topics to follow and receive notifications when relevant discussions occur. Notifications include an excerpt and a source link, and check the subscriber’s current access to the source channel. Conversation retrieval checks current permissions and stays within the channel and server where it is requested; DM retrieval is limited to the requester’s conversation with Vox.

Moderators can stop processing and clear a channel’s active archive with /vox channel disable, or resume processing with /vox channel enable. Administrators also control Vox’s access through Discord permissions. Removing Vox from a server automatically clears that server’s active archive and feature records, including reconciliation after an offline removal.

Any member can use /vox privacy for the public policy, contact, opt-out command, and deletion instructions. /vox optout opens a private confirmation; confirming stops processing the member’s authored messages and voice across servers and DMs, removes active indexed messages and personal feature records, and disables their Vox access. Minimal identifiers preserve that choice across restarts.

The operator is Wabbit Consulting Corporation. Privacy and deletion requests: wabbit@wabbit.one.

Feature screenshots: https://vox.wabbit.one/evidence/2026-10-08/

(1774 / 2000 characters)

## Where is your Privacy Policy available?

Any member can run /vox privacy in Discord to receive a private response with the public privacy-policy link, wabbit@wabbit.one contact, /vox optout command, deletion instructions, and moderator channel controls. The command remains available to opted-out members. The policy is also linked from the public project README. The public Vox website at https://vox.wabbit.one also links to the policy.

(397 / 2000 characters)

## Privacy Policy link

https://github.com/wabbit-corp/python-jeeves/blob/master/PRIVACY.md

(67 / 2000 characters)

## How do users request deletion?

Email wabbit@wabbit.one privately with your Discord user ID and the scope of your access, deletion, or processing-restriction request. We respond within one month. Verified deletion requests cover the personal data we hold or control, including active databases, feature records, logs, transcripts, exports, and recovery copies; only minimal opt-out identifiers remain to honor the choice.

/vox optout provides a private confirmation to stop processing authored messages and voice, remove active indexed messages and personal feature records, and disable Vox access across servers and DMs. The command does not by itself delete historical recovery copies or provider-held records; users can request their deletion at the email address above.

(742 / 2000 characters)

## Why do you need Message Content?

Vox’s core features depend on understanding discussions across multiple messages and participants, including messages that do not mention the bot. For example, a member can ask “vox, summarize the options we discussed” after several members have compared options. Vox needs those preceding messages to produce a useful answer. Conversation retrieval similarly needs the earlier discussion the member asks it to find.

Members choose topics they want to follow. Vox identifies relevant new discussions in accessible channels and notifies subscribers who currently have access to the source channel. Message Content lets Vox recognize those discussions as they happen, including messages addressed to other community members.

Members can invoke Vox through @Vox, DMs, or “v” and “vox”. Message Content supplies the surrounding discussion needed for contextual summaries and retrieval even when the request itself mentions @Vox. We request only Message Content for these community features.

The archive supports continuing retrieval of earlier community discussions while Vox serves the server. Moderators can stop processing and clear a channel’s active archive with /vox channel disable, and members can opt out with /vox optout or request deletion at wabbit@wabbit.one. Removing Vox from a server automatically clears that server’s active archive and related feature records. We also delete information when it no longer serves these operational purposes or when required by law or Discord.

(1492 / 2000 characters)

## Evidence links and explanation

Public evidence gallery (no Discord login needed):
https://vox.wabbit.one/evidence/2026-10-08/

Contextual answers — exhibits 1–2:
https://vox.wabbit.one/evidence/2026-10-08/#exhibit-1
In The Wabbitat, one member asks a question without addressing Vox; another says “v ^”, and Vox answers that earlier question. In mememaps.net, Vox refers back to an earlier participant’s unmentioned question. These examples show why surrounding message content is needed even when the invoking message addresses Vox.

Conversation retrieval — exhibits 3–4:
https://vox.wabbit.one/evidence/2026-10-08/#exhibit-3
Vox retrieves earlier messages with source links. Opening a returned link shows the original historical message in the same channel.

Topic notification — exhibit 5:
https://vox.wabbit.one/evidence/2026-10-08/#exhibit-5
A delivered historical notification includes an excerpt from a relevant discussion and a source link. The matched member message did not mention Vox.

Deployed privacy controls — exhibits 6–8:
https://vox.wabbit.one/evidence/2026-10-08/#exhibit-6
Private /vox privacy response, /vox optout confirmation screen, and moderator channel status. The opt-out preview was cancelled after capture.

The gallery includes privacy-redacted full-resolution screenshots, dates, captions, and source links for application 1382045038501953729.

(1345 / 2000 characters)

## Internal verification notes

- All four live SQLCipher databases passed integrity and page-authentication checks after restart.
- All 93 retired files were verified locally before production deletion; the retained server archive passed a streaming decryption/hash check. Local recovery copies remain on a FileVault-encrypted workstation.
- Diagnostic journals and temporary exports use bounded memory storage; host swap is encrypted and activated correctly after boot. Persistent voice transcript files are disabled.
- One memory-heavy archive audit required a host recovery restart. The corrected audit reads only the header; Vox reconnected with no errors, and the evidence site returned HTTPS 200.
- The opt-out screenshot is a confirmation preview, cancelled after capture. The topic notification is historical supporting evidence; its original source channel is currently inaccessible to the capturing account.
- Previous focused application tests: 138 passed. The canonical dev check remains unsuccessful because of the existing local environment/convention issues recorded in /tmp/vox-deploy-dev-check.log. No application source changed in this final storage/documentation pass.
- The updated privacy policy was published in commit e89615d.

Review the entire form, policy, and screenshots before checking the acknowledgement and pressing Submit yourself. Nothing has been submitted.
