from library import Library, read_file
import anthropic
from dotenv import load_dotenv
import os
import json

# This class is used for Paige's writing functionality.
class Scribe :
    def __init__(self, paige_search, library, client, write_path=None):
        self.write_path = write_path
        self.search = paige_search
        self.library = library
        self.client = client

    # Compares a piece of text (A query, a chunk, etc.) to the library. Returns a decision condition and a rationale.
    def compare(self, text):
        SYSTEM_PROMPT = """
            You are Paige, a worldbuilding librarian for a fictional world's wiki. 
            Your task is to compare the 'COMPARE_TEXT' with the 'ARTICLES' and determine any discrepencies or contradictions between them.
            The ARTICLES are wiki pages deemed relevant to the compare text. Assume the compare text is from a more official document (the rough draft, new worldbuilding notes, etc.)
            and that the ARTICLES may be outdated. 
            Return a JSON array of two values. The first is the DECISION_CODE, one of the following listed below. The second value is the RATIONALE, the reasoning behind your choice and the suggestion to be made. 
            The RATIONALE should be kept professional, precise and knowledgeable in tone. 

            Decision Codes:
            R - Resolute: No reconciliation is required. The COMPARE_TEXT matches the current knowledge base. 
            U - Update: Reconciliation required. There is some discrepency between the COMPARE_TEXT and the ARTICLES
            N - Null: The COMPARE_TEXT does not feel relevant to the ARTICLES.

            RETURN EXAMPLE:
            ["R", "No changes required."]
            Do not return anything except for the array.
        """
        context = f"COMPARE_TEXT: {text}\n --- END-OF-COMPARE-TEXT --- \n\n"
        relevant_files = self.search(text)
        for file in relevant_files:
            context = context + f"\n --- Article: {file.stem} ---\n {read_file(file)} \n --- END-OF-FILE --- \n\n"

        try:
            message = self.client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": context}],
            )
            raw = message.content[0].text.strip()
            print(f"Paige: {json.loads(raw)}")
        
        except json.JSONDecodeError:
            print(f"Paige: Unable to parse returned JSON {raw}")
        
        except Exception as e:
            print(f"[Compare] Claude API error: {e}")

        return
        
        

    def restructure(self, file):
        # Takes a wiki file and restructures it to match the current standard. 
        return
    
    def create_article(self, title, text):
        file_path = self.write_path + "\\" + title + ".md"
        try:
            with open(file_path, "x") as file:
                file.write(text)
                print(f"Paige: Article: {title} successfully created.")
        except FileExistsError:
            print(f"Paige: A file with title: {title} already exists. The new file has not been created.")

    
    def write_article(self, text):
        SYSTEM_PROMPT = """
            You are Paige, a worldbuilding librarian for a fictional world's wiki. 
            Your task is to take the given text and relevant files and create a wiki article about what the user requests. 
            Prioritize the article description over the context, weighing the context in for world understanding and depth. 
            Write in a traditional fantasy wiki tone, serious but not academic. Ensure the writing is concise and easily able to parse. Do not use em-dashes or other AI-generated text quirks.
            Write the article in traditional markdown for the text. Do not include \n, instead use enter. Feel free to create headers or subheaders for various sections.
            For any other names or proper nouns deserving of a wiki article, place [[]] around their name for Obsidian to make them links.
            The article's length should vary depending on the amount of information and content available to you. If there is a lack of information, feel free to keep the article short.
            NEVER create information or expand upon lore outside of the original context, although you may elaborate or change wording to make the article sound better.

            Return the article in the following structure EXACTLY: 
            Example: "Title &&& Article_text"
            This is needed for proper parsing.
        """
        context = f"GIVEN_TEXT: {text}\n --- END-OF-GIVEN-TEXT --- \n\n"
        relevant_files = self.search(text)
        for file in relevant_files:
            context = context + f"\n --- Article: {file.stem} ---\n {read_file(file)} \n --- END-OF-FILE --- \n\n"

        try:
            message = self.client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=1000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": context}],
            )
            raw = message.content[0].text.strip()
            ret = raw.split("&&&")
            title = ret[0]
            content = ret[1]
            self.create_article(title, content)

        except Exception as e:
            print(f"[Write] Claude API error: {e}")

        

        return
    
    def delete_article(self):
        return
    
    def reconcile(self):
        return