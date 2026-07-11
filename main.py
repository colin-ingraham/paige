import sys

from paige import Paige
from drive import DriveError


# Ensure unicode from the LLM (em-dashes, smart quotes) prints cleanly on Windows,
# where the console/pipe would otherwise default to cp1252 and mangle it.
sys.stdout.reconfigure(encoding="utf-8")

main_path = "I:/Terrastis Wiki"

HELP_TEXT = """Commands:
  /help                  Show this help
  /sources               List the local dirs and Drive folders Paige indexes
  /addsource <path>      Add a local directory to the index
  /removesource <path>   Remove a local directory from the index
  /drive                 Show Google Drive connection status
  /driveconnect          Authorize Google Drive access
  /adddrive <url|id>     Add a Google Drive folder as a source
  /removedrive <id>      Remove a Google Drive folder source
  /syncdrive             Re-sync all Drive folders and refresh the index
  /reindex               Rebuild the entire index from scratch
  /clear                 Clear the current conversation memory
  /exit, /quit           Quit Paige
Anything else is treated as a question."""


# Renders a Drive sync counts dict (added/updated/unchanged/removed/skipped) into
# a short, human-readable summary line.
def format_counts(counts):
    parts = [f"{counts.get(k, 0)} {k}" for k in
             ("added", "updated", "unchanged", "removed", "skipped")]
    return ", ".join(parts)


# Parses and runs a slash command, delegating the real work to Paige.
# Returns False only when Paige should exit, True otherwise.
def handle_command(paige, user_input):
    parts = user_input.split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/exit", "/quit"):
        print("Paige :: Goodbye!")
        return False
    elif command == "/help":
        print(HELP_TEXT)
    elif command == "/sources":
        sources = paige.list_sources()
        drive_sources = paige.list_drive_sources()
        if sources or drive_sources:
            if sources:
                print("Paige :: Local sources:")
                for source in sources:
                    print(f"  - {source}")
            if drive_sources:
                print("Paige :: Google Drive folders:")
                for source in drive_sources:
                    print(f"  - {source['name']} ({source['folder_id']})")
        else:
            print("Paige :: No sources configured. Add one with /addsource <path> "
                  "or /adddrive <folder url>.")
    elif command == "/addsource":
        if not arg:
            print("Paige :: Usage: /addsource <path>")
        elif paige.add_source(arg):
            print(f"Paige :: Added and indexed: {arg}")
        else:
            print(f"Paige :: Already indexing: {arg}")
    elif command == "/removesource":
        if not arg:
            print("Paige :: Usage: /removesource <path>")
        elif paige.remove_source(arg):
            print(f"Paige :: Removed: {arg}")
        else:
            print(f"Paige :: Not an indexed source: {arg}")
    elif command == "/drive":
        if paige.drive_connected():
            print("Paige :: Google Drive is connected.")
        else:
            print("Paige :: Google Drive is not connected. Run /driveconnect to authorize.")
    elif command == "/driveconnect":
        try:
            paige.connect_drive()
            print("Paige :: Google Drive authorized.")
        except DriveError as e:
            print(f"Paige :: {e}")
    elif command == "/adddrive":
        if not arg:
            print("Paige :: Usage: /adddrive <folder url or id>")
        else:
            try:
                print("Paige :: Syncing Drive folder (this may open a browser to authorize)...")
                counts = paige.add_drive_source(arg)
                if counts is None:
                    print("Paige :: Already tracking that Drive folder.")
                else:
                    print(f"Paige :: Added Drive folder. {format_counts(counts)}.")
            except DriveError as e:
                print(f"Paige :: {e}")
    elif command == "/removedrive":
        if not arg:
            print("Paige :: Usage: /removedrive <folder id>")
        elif paige.remove_drive_source(arg):
            print(f"Paige :: Removed Drive folder: {arg}")
        else:
            print(f"Paige :: Not a tracked Drive folder: {arg}")
    elif command == "/syncdrive":
        drive_sources = paige.list_drive_sources()
        if not drive_sources:
            print("Paige :: No Drive folders configured. Add one with /adddrive <url>.")
        else:
            try:
                print("Paige :: Syncing Drive folders...")
                results = paige.sync_drive()
                for folder_id, counts in results.items():
                    print(f"  - {folder_id}: {format_counts(counts)}")
            except DriveError as e:
                print(f"Paige :: {e}")
    elif command == "/reindex":
        print("Paige :: Rebuilding the index...")
        paige.reindex()
        print("Paige :: Index rebuilt.")
    elif command == "/clear":
        paige.clear_memory()
        print("Paige :: Conversation memory cleared.")
    else:
        print(f"Paige :: Unknown command '{command}'. Type /help for options.")

    return True


try:
    paige = Paige(main_path)
except RuntimeError as e:
    print(f"Paige :: {e}")
    raise SystemExit(1)

missing = paige.missing_sources()
if missing:
    print(f"Paige :: Heads up — these sources aren't reachable right now: {', '.join(missing)}")

print("Paige :: Welcome! Ask a question, or type /help for commands.")

while True:
    try:
        user_input = input("User :: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nPaige :: Goodbye!")
        break

    if not user_input:
        continue

    if user_input.startswith("/"):
        if not handle_command(paige, user_input):
            break
        continue

    print("Paige :: ", end="", flush=True)
    for chunk in paige.ask_paige(user_input):
        print(chunk, end="", flush=True)
    print()
