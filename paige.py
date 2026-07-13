from library import Library, read_file
from scribe import Scribe
import anthropic
from dotenv import load_dotenv
import os
import json


class Paige:

    # Cap on how many conversation messages to retain so long sessions don't grow
    # the request (and cost) without bound. Messages alternate user/assistant, so
    # this keeps roughly the last MAX_HISTORY_MESSAGES / 2 question-answer turns.
    MAX_HISTORY_MESSAGES = 20

    def __init__(self):
        load_dotenv()
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env file before running Paige."
            )
        self.client = anthropic.Anthropic()
        self.library = Library()
        self.scribe = Scribe(self.search, self.library, self.client)
        if not self.library.get_sources():
            print(f"Paige :: Welcome to Paige! In order to continue, please first add a main path for Paige to reference.")
            self.library.add_source(input("User:: "))

        # Plain question/answer turns only (no retrieved-article text), so follow-up
        # questions retain context without the history bloating every later request.
        # A caller can seed prior turns (e.g. reloaded from a database) so
        # conversations survive restarts; the cap still applies.
        self.history = []

    def ask_paige(self, prompt):
        # Rewrite follow-ups into a standalone query so retrieval uses the right context,
        # but answer the user's original wording. Yields the answer in streamed chunks.
        search_query = self.contextualize_query(prompt)
        file_set = self.search(search_query)
        yield from self.answer(prompt, file_set)

    # Formats the stored question/answer history into a plain transcript for the
    # query-rewriting step.
    def recent_exchange(self):
        lines = []
        for message in self.history:
            speaker = "User" if message["role"] == "user" else "Paige"
            lines.append(f"{speaker}: {message['content']}")
        return "\n".join(lines)

    # Rewrites a follow-up question into a single standalone search query, resolving
    # pronouns and references against the conversation so retrieval targets the right
    # entities. Returns the prompt unchanged when there is no prior context.
    def contextualize_query(self, prompt):
        if not self.history:
            return prompt

        SYSTEM_PROMPT = """
            You rewrite a user's follow-up question into a single standalone search query
            for a worldbuilding wiki. Use the conversation so far to resolve pronouns and
            references (e.g. 'them', 'there', 'the father', 'that war') into the specific
            named entities they refer to.
            Output only the rewritten query as plain text — no quotes, no explanation, no
            markdown. If the question is already self-contained, return it unchanged.
        """

        transcript = self.recent_exchange()
        user_content = (
            f"Conversation so far:\n{transcript}\n\n"
            f"Follow-up question: {prompt}\n\n"
            f"Standalone search query:"
        )

        try:
            message = self.client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=200,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            return message.content[0].text.strip()

        except Exception as e:
            # On any failure, fall back to searching the raw follow-up
            print(f"[Contextualize] Claude API error: {e}")
            return prompt

    # Clears the running conversation history so the next question starts fresh.
    def clear_memory(self):
        self.history = []

    # Returns the source directories Paige currently indexes.
    def list_sources(self):
        return self.library.get_sources()

    # Returns configured sources that are not reachable right now.
    def missing_sources(self):
        return self.library.missing_sources()

    # Adds a directory for Paige to index; returns False if already present.
    def add_source(self, path):
        return self.library.add_source(path)

    # Removes a directory from Paige's index; returns False if it wasn't indexed.
    def remove_source(self, path):
        return self.library.remove_source(path)

    # Rebuilds the entire index from scratch across all current sources.
    def reindex(self):
        self.library.rebuild()

    # Returns the configured Drive folder sources ({folder_id, name}) Paige ingests.
    def list_drive_sources(self):
        return self.library.get_drive_sources()

    # Reports whether Drive access has already been authorized.
    def drive_connected(self):
        return self.library.drive_connected()

    # Runs the Drive OAuth flow up front so later syncs don't prompt.
    def connect_drive(self):
        return self.library.connect_drive()

    # Adds a Drive folder as a source and indexes it; returns sync counts, or None
    # if the folder is already tracked.
    def add_drive_source(self, folder_id, name=None):
        return self.library.add_drive_source(folder_id, name)

    # Removes a Drive folder source; returns False if it wasn't tracked.
    def remove_drive_source(self, folder_id):
        return self.library.remove_drive_source(folder_id)

    # Re-syncs every Drive folder and refreshes the index; returns per-folder counts.
    def sync_drive(self):
        return self.library.sync_drive()

    def search(self, prompt, top_n=5):
        # Refresh the index before searching so newly edited wiki files are reflected
        self.library.process_files()
        scores = {}

        keywords = self.extract_keywords(prompt)
        if keywords:
            for keyword in keywords:
                files = self.library.keyphrase_lookup(keyword)
                for path, freq in files.items():
                    scores[path] = scores.get(path, 0) + (freq * 0.5)

        vector_files = self.library.vector_search(prompt)
        for path, (chunk_count, min_distance) in vector_files.items():
            vector_score = (2 - min_distance)
            vector_score += chunk_count * 0.3
            if path in scores:
                vector_score *= 1.5
            scores[path] = scores.get(path, 0) + vector_score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        # Drop any path that no longer exists on disk so a stale index entry can't
        # crash the answer step; keep collecting until we have up to top_n valid files.
        return [path for path, score in ranked if path.exists()][:top_n]

    # Streams Paige's answer as text chunks, grounded in the given article files, and
    # records the completed turn in history once the full reply has been received.
    def answer(self, question, files):
        SYSTEM_PROMPT = """
            You are Paige, a worldbuilding librarian for a fictional world's wiki.
            Answer using only the article content provided below; it has been retrieved from
            the wiki and holds the relevant information if any exists.
            If the provided articles do not answer the question, say plainly that the wiki has
            no information on it. Never invent lore that isn't in the articles.
            Write in a calm, precise, knowledgeable tone — like a well-read archivist. Be
            concise and factual rather than effusive; skip filler enthusiasm and exclamations.
            When a statement draws on an article, cite that article by its title in
            parentheses, for example (Terrastis).
        """

        context = ""
        for file in files:
            context = context + f"\n --- Article: {file.stem} ---\n {read_file(file)} \n --- END-OF-FILE --- \n\n"

        # Send the prior plain history plus this turn's freshly retrieved articles. The
        # articles live only in this request, not in the persisted history.
        current_turn = {"role": "user", "content": context + f"Question: {question}"}
        messages = self.history + [current_turn]

        reply_parts = []
        try:
            with self.client.messages.stream(
                model="claude-haiku-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    reply_parts.append(text)
                    yield text

        except Exception as e:
            print(f"[Answer] Claude API error: {e}")
            return

        # Persist only the plain question and answer, then trim to stay bounded
        reply = "".join(reply_parts).strip()
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": reply})
        self.history = self.history[-self.MAX_HISTORY_MESSAGES:]

    def extract_keywords(self, prompt):
        SYSTEM_PROMPT = """
            Take the following input prompt given by the user and return a JSON array of relevant named entities from it.
            Keywords can be either one word or multiple, so long as it is one collective entity. 
            Do not add conceptual words like 'exile' or 'greed', but instead names of people, locations, or items.
            Return only the raw JSON array, no markdown formatting, no code blocks, no explanation.
            If there are no named entities, return an empty array: []

            EXAMPLE:
            prompt: "Where in Terrastis did King Avonis begin the Small Conquering?"
            output: ["Terrastis", "King Avonis", "Small Conquering"]
        """

        try:
            message = self.client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            return json.loads(raw)

        except json.JSONDecodeError:
            # Model returned something unparseable - silently fall back to vector search
            return []

        except Exception as e:
            print(f"[Keyword Extraction] Claude API error: {e}")
            return []