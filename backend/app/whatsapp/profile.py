"""What customers see before they type: the business profile, first-chat suggestions and "/" commands.

Applied to the number with `python -m scripts.whatsapp_profile`. Every suggestion and command must be
something the bot understands: `tests/test_whatsapp_units.py` checks each one against `command_of`.
"""

from __future__ import annotations

# Shown under the business name (Meta allows 139 characters).
ABOUT = "Send me chat screenshots and I'll turn the deal into a receipt you can share."

# The business profile's description (Meta allows 512 characters).
DESCRIPTION = (
    "ChatReceipt turns screenshots of a WhatsApp, Instagram or SMS chat into a clear record of the deal: "
    "what was agreed, what has been paid and what is still owed. Send the screenshots here, check the details "
    "and confirm to get the receipt as an image and a PDF. ChatReceipt records what the chat says; it doesn't "
    "verify payments."
)

VERTICAL = "PROF_SERVICES"

# Suggestions WhatsApp shows when someone opens the chat for the first time; tapping one sends it as a
# message (Meta allows 4, of up to 80 characters).
PROMPTS = (
    "Turn a chat into a receipt",
    "How does ChatReceipt work?",
    "Show my recent records",
    "Open my records on the web",
)

# The menu shown when someone types "/" (Meta allows 30; names up to 32 characters, descriptions up to 256).
COMMANDS = (
    ("help", "How ChatReceipt works and what you can send"),
    ("new", "Start a new record"),
    ("show", "See the draft you're working on"),
    ("retry", "Read the screenshot again"),
    ("recent", "Your latest saved records"),
    ("web", "A private link to your records in a browser"),
    ("cancel", "Stop working on the current draft"),
)

LIMITS = {"about": 139, "description": 512, "prompts": 4, "prompt": 80, "commands": 30, "command_name": 32, "command_description": 256}
