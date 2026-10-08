# Conversation retrieval

Vox keeps an encrypted message index for contextual replies and conversation
retrieval. The background indexer runs with the bot and records messages only
where Discord permissions and Vox privacy controls permit processing.

History search and local indexed search recheck the requester’s current channel
permissions and keep results within the current server and channel. Member
opt-outs, moderator channel cleanup, and server-removal cleanup apply to the index.

Run the bot using the [deployment instructions](deploy/README.md). See the
[Vox Privacy Policy](PRIVACY.md) for retention, deletion, and contact details.
