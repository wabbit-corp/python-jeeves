# Vox Privacy Policy

Effective date: October 7, 2026

Vox is a Discord bot operated by Wabbit Consulting Corporation. This policy covers the operator's Vox service, Discord application ID **1382045038501953729**. It explains what Vox processes, why, and how to contact us about your data. Independently operated copies of the open-source software have their own operators and policies.

Privacy questions and requests: [vox@wabbit.one](mailto:vox@wabbit.one). You can also reach the operator at [wabbit@wabbit.one](mailto:wabbit@wabbit.one). Please contact us privately rather than posting personal information in a public GitHub issue.

## Information we process

- **Discord messages and discussions:** message text, author and message IDs, names, timestamps, reply references, mentions, reactions, attachment metadata and links, and channel and server IDs. Vox processes channels it can access and direct messages sent to it. This can include accessible history and messages that do not mention Vox.
- **Discord account and server information:** profile names and identifiers, channel details, server membership, roles, and permissions used to operate features and determine access. Depending on the Discord access enabled for the deployed bot, online status may also be supplied to the Discord client.
- **Feature records:** reminder and commitment details, progress notes, topic subscriptions, notification history, and requested calendar or video-feed subscriptions.
- **Voice, when enabled:** when a Discord voice-channel invite causes Vox to join a voice channel, Vox receives participants' audio and sends short audio segments to OpenAI for transcription. Audio is buffered in memory; Vox does not save raw audio recordings to disk. Transcribed text and participant, server, and channel identifiers can appear in service logs and configured transcript files. Participants should be told before Vox is invited into a voice channel.
- **Service and support records:** request activity, diagnostic logs that may contain message or response content, and information you send us when asking for help or exercising privacy rights.

Server permissions limit which channels Vox can access. Vox stores a searchable message index and feature records on its host and keeps recent conversation context in memory.

## How we use information

We use this information to answer questions, summarize discussions, retrieve earlier conversations, deliver requested notifications, manage reminders and commitments, transcribe voice when enabled, and operate and troubleshoot Vox.

Vox's answers and retrieved content can be posted in the Discord channel or direct message used for a feature. People with access to that destination can see the output. Server administrators with access to administrative features may also obtain exports of subscription and commitment records. Avoid sending Vox information you do not want processed or included in a reply.

## Services that receive information

DigitalOcean provides the server hosting Vox's stored messages and feature records. Discord carries messages, commands, voice connections, and bot replies. Relevant conversation context, requests, names or identifiers included in that context, image prompts, and voice segments may be sent to **OpenAI** to provide replies, image generation, or transcription.

When an external feature is enabled and used, the information needed for that feature is sent to its provider:

| Provider or destination | Information sent for the feature |
| --- | --- |
| Brave Search | Search terms needed for a web search. |
| Imgflip | Meme template choices and requested captions. |
| Open-Meteo | Location coordinates needed for a weather request. |
| Wikipedia and YouTube | Requested topics, video identifiers, or feed URLs needed to retrieve information. |
| GitHub | Issue or contribution text and code submitted through repository features; these can become public. |
| Websites, calendar feeds, and other requested URLs | The URL and normal network request information needed to fetch the resource. |

Each feature sends the inputs needed to perform that feature to its provider. Providers process that information under their applicable terms and privacy practices; their retention can differ from Vox's retention. Data may be processed in countries other than your own.

## Retention and deletion

Vox does not apply a fixed expiry period to indexed messages, feature records, service logs, or operator-held recovery copies. Older information can remain until we delete it. You can request deletion at any time; we also remove information when it is no longer needed to operate Vox or when deletion is required by law or Discord.

Deleting a message in Discord does not by itself erase Vox's stored copy. The current index can mark messages as deleted while retaining their content. Removing Vox from a server or restricting its channel access stops future access to those spaces, but does not automatically delete existing records.

To request access, correction, deletion, or restriction of processing, email [vox@wabbit.one](mailto:vox@wabbit.one) with your Discord user ID and the scope of your request. We may ask for enough information to verify that the account is yours. Do not send your Discord password, access token, or unnecessary sensitive information.

We handle requests promptly and respond within one month of receipt. If applicable law permits extra time for a complex request, we explain the reason and expected response date within that first month. For a verified deletion request, we remove the personal data we hold or control from active databases, feature records, logs, transcript files, and recovery copies, retaining only the minimal opt-out identifiers described below. We also notify relevant providers of the request and seek deletion of copies they process for Vox where applicable. We explain any lawful retention or provider limitations in our response.

Deleting information held by us does not delete messages held by Discord or copies independently retained by other participants. If a legal obligation requires us to retain particular information, we will explain that limitation where permitted.

Depending on the law that applies to you, you may also have rights to receive a portable copy of your personal data, object to processing, and complain to your local data protection authority. Contact the privacy address to exercise your rights or ask about our handling of a request.

## Opting out

You can contact the privacy address to request deletion or raise a processing concern. The automated opt-out command described below is awaiting deployment.

We are preparing **`/vox optout`**. At this policy's publication date, it has not yet been deployed. Once available, it will open a private confirmation visible only to the person invoking it. Confirming will stop processing that person's authored messages and voice across servers and DMs, remove their messages from the active index, stop their reminders and topic subscriptions, and disable all their Vox features. The preference will persist across restarts, with no user command to undo it.

The command will retain minimal Discord user and message identifiers to enforce the preference and prevent withdrawn messages from being restored by history indexing or partial edits. We retain those minimal identifiers after a deletion request so Vox can continue honoring the opt-out across restarts. They contain no message text, voice audio, reminder details, or subscription content.

The command alone will not erase historical logs, backups, information other people include in their own messages, or copies already sent to providers. Email the privacy contact to request deletion of the personal data we hold or control in those records.

## Security

Vox is hosted on a DigitalOcean server in New York, United States. The bot runs under a dedicated service account. Its data directories and database files use restricted filesystem permissions, and its runtime credentials are stored in a separate restricted configuration file. Administrative access uses SSH keys; password-based and direct root SSH login are disabled. Host and cloud firewalls restrict inbound access. Connections to Discord, OpenAI, and the HTTPS provider endpoints use encrypted transport.

At publication, the local databases use access controls but are not encrypted by the application or the host filesystem. Authorized administrators can read the stored information.

We have prepared a switch to SQLCipher encryption for the bot's databases, but it has not yet been deployed. This preparation does not encrypt the live databases, existing logs, transcript files, or older copies. We will update this policy when the deployed safeguards change.

No service can guarantee absolute security. Report suspected unauthorized access to your Vox data privately using the contact above so we can investigate and take appropriate action.

## Changes and contact

We will publish updates at this policy's GitHub location and change the effective date when the policy changes. Contact [vox@wabbit.one](mailto:vox@wabbit.one) with privacy questions, requests, or concerns about Vox.
