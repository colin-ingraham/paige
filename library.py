from pathlib import Path
import string
import json
import shutil
import chromadb

import drive


def get_timestamp(path):
    return path.stat().st_mtime

def read_file(path):
    with open(path, 'r', encoding='utf-8') as file:
        content = file.read()
        return content

class Library:

    # File types Paige knows how to index. All are plain text, so they share the
    # same read/chunk/embed path; binary formats would need their own parsers.
    SUPPORTED_EXTENSIONS = ['.md', '.markdown', '.txt', '.csv']

    def __init__(self, main_path=None, chroma_path=".chroma", workspace=".",
                 collection_name="paige", chroma_client=None, drive_token_path=None):
        
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.config_path = self.workspace / "config.json"
        self.data_path = self.workspace / "data.json"
        # Drive folders are mirrored here, one subdirectory per folder id, then
        # indexed like any other local source. This keeps retrieval/indexing
        # backend-agnostic: it only ever sees plain files on disk.
        self.drive_cache = self.workspace / ".drive_cache"
        # Per-workspace Drive token so each web user authorizes their own account;
        # None falls back to drive.py's default single-user token file.
        self.drive_token_path = drive_token_path

        config = self.load_config()
        self.sources = config["sources"]
        self.drive_sources = config["drive_sources"]

        # A caller (the web app) can share one Chroma client across many libraries,
        # separated by collection name; standalone use creates its own client.
        if chroma_client is None:
            chroma_client = chromadb.PersistentClient(path=chroma_path)
        self.collection = chroma_client.get_or_create_collection(name=collection_name)

        self.file_dict = {}
        self.keywords = {}
        self.keyword_index = {}

        self.data = self.load_json()
        self._sync_chroma()
        self.process_files()

    # Loads the configured local and Drive sources from config.json, seeding the
    # file with a default local path the first time so existing behavior is
    # preserved. Returns a dict with "sources" (local dirs) and "drive_sources"
    # (list of {folder_id, name}).
    def load_config(self, default_source=None):
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
            return {
                "sources": config.get("sources", []),
                "drive_sources": config.get("drive_sources", []),
            }
        except (FileNotFoundError, json.JSONDecodeError):
            sources = [str(Path(default_source))] if default_source else []
            config = {"sources": sources, "drive_sources": []}
            self.sources = sources
            self.drive_sources = []
            self.save_config()
            return config

    # Persists the current local and Drive sources to config.json.
    def save_config(self):
        with open(self.config_path, 'w') as f:
            json.dump(
                {"sources": self.sources, "drive_sources": self.drive_sources},
                f, indent=4,
            )

    # Returns the source directories currently configured.
    def get_sources(self):
        return self.sources

    # Returns the configured sources that exist right now (skips e.g. an unmounted drive).
    def accessible_sources(self):
        return [str(Path(s)) for s in self.sources if Path(s).exists()]

    # Returns the configured sources that are not currently reachable.
    def missing_sources(self):
        return [s for s in self.sources if not Path(s).exists()]

    # Adds a directory to the indexed sources, persists it, and refreshes the index.
    def add_source(self, path):
        normalized = str(Path(path))
        if normalized in self.sources:
            return False
        self.sources.append(normalized)
        self.save_config()
        self.process_files()
        return True

    # Removes a directory from the indexed sources and purges its files from the index.
    def remove_source(self, path):
        normalized = str(Path(path))
        if normalized not in self.sources:
            return False
        self.sources.remove(normalized)
        self.save_config()
        # Purge the removed source's files explicitly; process_files only deletes
        # files under sources that are still accessible, so it would leave these behind.
        self.purge_under(normalized)
        return True

    # Drops every indexed file whose path starts with `prefix` from the index
    # (data.json, keyword tables, and Chroma) in one place, so source removal and
    # Drive-folder removal share identical cleanup.
    def purge_under(self, prefix):
        purged = [p for p in self.file_dict if str(p).startswith(prefix)]
        for path in purged:
            self.remove_from_save(path)
            self.remove_keywords(path)
            self.remove_embedding(path)
            del self.file_dict[path]
        if purged:
            self.save_to_json()

    # Returns the cache directory that mirrors a given Drive folder's files.
    def cache_dir_for(self, folder_id):
        return self.drive_cache / folder_id

    # Returns the configured Drive folders ({folder_id, name}) Paige ingests.
    def get_drive_sources(self):
        return self.drive_sources

    # Reports whether a cached Drive authorization already exists.
    def drive_connected(self):
        return drive.is_connected(token_path=self.drive_token_path)

    # Runs the Drive OAuth flow up front so later syncs are non-interactive.
    def connect_drive(self):
        return drive.connect(token_path=self.drive_token_path)

    # Mirrors a single Drive folder into its cache directory (network call).
    def sync_drive_source(self, folder_id):
        return drive.sync_folder(folder_id, self.cache_dir_for(folder_id),
                                 token_path=self.drive_token_path)

    # Re-mirrors every configured Drive folder, then refreshes the index so any
    # added/changed/removed Drive docs are reflected. Returns per-folder counts.
    def sync_drive(self):
        results = {}
        for source in self.drive_sources:
            results[source["folder_id"]] = self.sync_drive_source(source["folder_id"])
        self.process_files()
        return results

    # Adds a Drive folder as a source: mirrors it once, persists it, and indexes the
    # mirrored files. Returns the sync counts, or None if the folder is already
    # tracked. The folder is only persisted after a successful first sync.
    def add_drive_source(self, folder_id, name=None):
        folder_id = drive.extract_folder_id(folder_id)
        if any(s["folder_id"] == folder_id for s in self.drive_sources):
            return None
        entry = {"folder_id": folder_id, "name": name or folder_id}
        self.drive_sources.append(entry)
        try:
            counts = self.sync_drive_source(folder_id)
        except Exception:
            self.drive_sources.remove(entry)
            raise
        self.save_config()
        self.process_files()
        return counts

    # Removes a Drive folder source: purges its mirrored files from the index and
    # deletes the cache directory. Returns False if the folder wasn't tracked.
    def remove_drive_source(self, folder_id):
        folder_id = drive.extract_folder_id(folder_id)
        match = next((s for s in self.drive_sources if s["folder_id"] == folder_id), None)
        if match is None:
            return False
        self.drive_sources.remove(match)
        self.save_config()
        cache = self.cache_dir_for(folder_id)
        self.purge_under(str(cache))
        if cache.exists():
            shutil.rmtree(cache, ignore_errors=True)
        return True

    # Wipes the entire index (Chroma + in-memory tables + data.json) and rebuilds it
    # from scratch across all current sources. A safe escape hatch for index drift.
    def rebuild(self):
        existing_ids = self.collection.get()['ids']
        if existing_ids:
            self.collection.delete(ids=existing_ids)
        self.file_dict = {}
        self.keywords = {}
        self.keyword_index = {}
        self.data = self.create_empty_data()
        self.save_to_json()
        self.process_files()

    def _sync_chroma(self):
        # If file_dict has entries but Chroma is empty, embeddings were lost - rebuild them
        if len(self.file_dict) > 0 and self.collection.count() == 0:
            for path in self.file_dict:
                self.embed_file(path)

    # Returns every root directory currently indexed: accessible local sources plus
    # any Drive cache directories that have been mirrored to disk. This is the single
    # list collect_files and the deletion check walk, so local and Drive sources flow
    # through exactly the same indexing path.
    def indexed_roots(self):
        roots = self.accessible_sources()
        for source in self.drive_sources:
            cache = self.cache_dir_for(source["folder_id"])
            if cache.exists():
                roots.append(str(cache))
        return roots

    # Gathers every indexable file across all indexed roots and supported types.
    def collect_files(self):
        files = []
        for root in self.indexed_roots():
            for ext in self.SUPPORTED_EXTENSIONS:
                files.extend(Path(root).rglob(f'*{ext}'))
        return files

    def process_files(self):
        found_files = self.collect_files()
        found_set = set(found_files)
        accessible = self.indexed_roots()
        rewrite = False
        for file in found_files:
            if file in self.file_dict:
                new_timestamp = get_timestamp(file)
                if not self.file_dict[file].compare_timestamp(new_timestamp):
                    rewrite = True
                    self.file_dict[file].timestamp = new_timestamp
                    self.reset_keywords(file)
                    self.reset_embedding(file)
            else:
                rewrite = True
                self.file_dict[file] = FileRegistry.from_path(file)
                self.add_keywords(file)
                self.embed_file(file)

        # Only treat a tracked file as deleted if its source is currently accessible;
        # otherwise an unmounted drive would cause its whole index to be wiped.
        deleted = {p for p in self.file_dict
                   if p not in found_set and any(str(p).startswith(a) for a in accessible)}
        if deleted:
            rewrite = True
            for path in deleted:
                self.remove_from_save(path)
                self.remove_keywords(path)
                self.remove_embedding(path)
                del self.file_dict[path]

        if rewrite:
            self.save_to_json()

    def chunk_file(self, path):
        text = read_file(path)
        content = text.splitlines()
        chunks = []
        current_chunk = ""
        for line in content:
            if line.startswith("#"):
                if current_chunk.strip():  # Only append if chunk has real content
                    chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk = current_chunk + " " + line
        if current_chunk.strip():  # Append final chunk if it has content
            chunks.append(current_chunk)

        return chunks

    def embed_file(self, path):
        chunks = self.chunk_file(path)
        if not chunks:
            return
        ids = []
        metadatas = []
        for i in range(len(chunks)):
            ids.append(str(path) + "##" + str(i))
            # Tag each chunk with its source so it can be removed without re-reading the file
            metadatas.append({"source": str(path)})
        self.collection.add(
            ids=ids,
            documents=chunks,
            metadatas=metadatas
        )

    # Removes every embedded chunk for a file by matching its id prefix. Works even
    # after the file is deleted, regardless of chunk count, and also clears legacy
    # embeddings written before chunks carried source metadata.
    def remove_embedding(self, path):
        prefix = str(path) + "##"
        existing_ids = self.collection.get()['ids']
        ids = [chunk_id for chunk_id in existing_ids if chunk_id.startswith(prefix)]
        if ids:
            self.collection.delete(ids=ids)

    def reset_embedding(self, path):
        self.remove_embedding(path)
        self.embed_file(path)

    def strip_id(self, id):
        return id.rsplit('##', 1)

    def add_keywords(self, path):
        content = read_file(path)
        translator = str.maketrans('', '', string.punctuation)
        content = content.translate(translator)

        for index, word in enumerate(content.split()):
            word = word.lower()
            self.keywords.setdefault(word, []).append(KeywordSource(path, index))
            self.keyword_index.setdefault(path, set()).add(word)

        self.add_to_save(path)

    def remove_keywords(self, path):
        if path in self.keyword_index:
            word_set = self.keyword_index[path]
            for word in word_set:
                self.keywords[word] = [ks for ks in self.keywords[word] if ks.path != path]
            del self.keyword_index[path]

    def reset_keywords(self, path):
        self.remove_keywords(path)
        self.add_keywords(path)

    def vector_search(self, keyphrase):
        results = self.collection.query(
            query_texts=[keyphrase],
            n_results=5
        )

        ret = {}
        for i in range(len(results['ids'][0])):
            path = Path(self.strip_id(results['ids'][0][i])[0])

            if path in ret:
                ret[path][0] += 1
            else:
                ret[path] = [1, results['distances'][0][i]]

        return ret

    def keyphrase_lookup(self, keyphrase):
        files = []
        keywords = keyphrase.split(' ')
        key_dict = {}

        for kw in keywords:
            r = self.keyword_lookup(kw)
            if not r:
                return {}
            else:
                key_dict[kw] = r

        for x in key_dict[keywords[0]]:
            path = x.path
            index = x.index
            count = 0
            for i in range(1, len(keywords)):
                valids = {ks for ks in key_dict[keywords[i]] if ks.path == path and ks.index == index + i}
                if valids:
                    count += 1
                else:
                    break

            if count == len(keywords) - 1:
                files.append(path)

        ret = {}
        for file in files:
            if file in ret:
                ret[file] += 1
            else:
                ret[file] = 1

        return ret

    def keyword_lookup(self, keyword):
        keyword = keyword.lower()
        files = []
        if keyword in self.keywords:
            path_list = self.keywords[keyword]
            for path in path_list:
                files.append(KeywordSource(path.path, path.index))

        return files

    def sort_by_freq(self, files):
        sorted_files = dict(sorted(files.items(), key=lambda item: item[1], reverse=True))
        return sorted_files

    def print_keyphrase_lookup(self, keyphrase):
        files = self.keyphrase_lookup(keyphrase)
        files = self.sort_by_freq(files)
        if not files:
            print(f"Keyphrase: '{keyphrase}' not found in files.")
        else:
            print(f"{len(files)} Articles found containing '{keyphrase}'")
            for path in files:
                path_title = path.stem
                print(f"- {path_title} ({files[path]})")

    def print_vector_search(self, keyphrase):
        files = self.vector_search(keyphrase)
        print(f"{len(files)} Articles found relating to '{keyphrase}'")
        for path in files:
            print(f"- {path} --- Frequency: {files[path][0]} --- Minimum Distance: ({files[path][1]})")

    def print_file_dict(self):
        print(self.file_dict)

    def print_keywords(self):
        print(self.keywords.keys())

    def load_json(self):
        try:
            with open(self.data_path, 'r') as file:
                data = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            data = self.create_empty_data()

        file_dict = data["file_dict"]
        for path in file_dict:
            self.file_dict[Path(path)] = FileRegistry(Path(path), file_dict[path])

        keywords = data["keywords"]
        for kw in keywords:
            self.keywords[kw] = []
            for path_str, indices in keywords[kw].items():
                for index in indices:
                    self.keywords[kw].append(KeywordSource(Path(path_str), index))

        keyword_index = data["keyword_index"]
        for path in keyword_index:
            self.keyword_index[Path(path)] = set()
            for word in keyword_index[path]:
                self.keyword_index[Path(path)].add(word)

        return data

    def add_to_save(self, file):
        self.data["file_dict"][str(self.file_dict[file].path)] = self.file_dict[file].timestamp

        if file in self.keyword_index:
            keyword_list = self.keyword_index[file]
            for keyword in keyword_list:

                if keyword not in self.data["keywords"]:
                    self.data["keywords"][keyword] = {}

                for ks in self.keywords[keyword]:
                    if str(ks.path) == str(file):
                        if str(ks.path) in self.data["keywords"][keyword]:
                            if ks.index not in self.data["keywords"][keyword][str(ks.path)]:
                                self.data["keywords"][keyword][str(ks.path)].append(ks.index)
                        else:
                            self.data["keywords"][keyword][str(ks.path)] = [ks.index]

            if str(file) not in self.data["keyword_index"]:
                self.data["keyword_index"][str(file)] = []

            for val in self.keyword_index[file]:
                self.data["keyword_index"][str(file)].append(val)

    def remove_from_save(self, file):
        del self.data["file_dict"][str(file)]

        if file in self.keyword_index:
            keyword_list = self.keyword_index[file]
            for keyword in keyword_list:
                del self.data["keywords"][keyword][str(file)]

        if file in self.keyword_index:
            del self.data["keyword_index"][str(file)]

    def save_to_json(self):
        with open(self.data_path, "w") as f:
            json.dump(self.data, f, indent=4)

    def create_empty_data(self):
        data = {
            "file_dict": {},
            "keywords": {},
            "keyword_index": {}
        }
        return data


class FileRegistry:

    def __init__(self, path, timestamp):
        self.path = path
        self.timestamp = timestamp

    @classmethod
    def from_path(cls, path):
        ts = get_timestamp(path)
        return cls(path, ts)

    def get_title(self):
        return self.path.stem

    def compare_timestamp(self, other_timestamp):
        return self.timestamp == other_timestamp


class KeywordSource:

    def __init__(self, path, index):
        self.path = path
        self.index = index

    def get_title(self):
        return self.path.stem