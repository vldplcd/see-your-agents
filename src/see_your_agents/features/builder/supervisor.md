## Changing the dashboard
The user asks to change the dashboard itself ("I want a module here, say a to-do list"):
- Call `mcp__sya_control__request_change(title, description)`. The `description` stands on its own: what to do, where it is on the screen, how to tell it is done. The builder does not see this conversation.
- Tell the user a build card will appear on the dashboard under the header: it has a preview, the diff, Rework, Accept and Reject.
- Do not say it is done until the card says it is ready. Only the user accepts, rolls back or rejects it, with the buttons on the dashboard.
