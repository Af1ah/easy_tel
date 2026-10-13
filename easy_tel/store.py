import sqlite3
import time


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, message INTEGER NOT NULL,
            name TEXT NOT NULL, size INTEGER NOT NULL, status TEXT NOT NULL,
            UNIQUE(chat, message))''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        for name, definition in {'attempts':'INTEGER NOT NULL DEFAULT 0',
                                 'next_attempt':'REAL NOT NULL DEFAULT 0',
                                 'progress_message':'INTEGER', 'error':'TEXT',
                                 'remote_path':'TEXT', 'remote_id':'TEXT', 'remote_md5':'TEXT', 'download_url':'TEXT'}.items():
            if name not in columns:
                self.db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
        self.db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        self.db.execute("UPDATE jobs SET status='done' WHERE status='deleting'")
        self.db.commit()

    def add(self, chat, message, name, size):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(chat,message,name,size,status) VALUES(?,?,?,?,'queued')",
                            (chat, message, name, size))
        return self.db.execute('SELECT * FROM jobs WHERE chat=? AND message=?', (chat, message)).fetchone()

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','retry_wait')").fetchone()[0]

    def next(self, now=None):
        return self.db.execute("SELECT * FROM jobs WHERE status IN ('queued','retry_wait') AND next_attempt<=? ORDER BY id LIMIT 1",
                               (time.time() if now is None else now,)).fetchone()

    def wait_seconds(self):
        row = self.db.execute("SELECT min(next_attempt) FROM jobs WHERE status IN ('queued','retry_wait')").fetchone()
        return max(.1, row[0] - time.time()) if row[0] is not None else 60

    def status(self, job_id, status):
        with self.db:
            self.db.execute('UPDATE jobs SET status=? WHERE id=?', (status, job_id))

    def begin(self, job_id):
        with self.db:
            self.db.execute("UPDATE jobs SET status='running', attempts=attempts+1 WHERE id=?", (job_id,))

    def defer(self, job_id, delay, error):
        with self.db:
            self.db.execute("UPDATE jobs SET status='retry_wait',next_attempt=?,error=? WHERE id=?",
                            (time.time()+delay, error, job_id))

    def save_progress(self, job_id, message_id):
        with self.db:
            self.db.execute('UPDATE jobs SET progress_message=? WHERE id=?', (message_id, job_id))

    def retry(self, job_id, chat):
        with self.db:
            result = self.db.execute("UPDATE jobs SET status='queued',attempts=0,next_attempt=0,error=NULL WHERE id=? AND chat=? AND status='failed'", (job_id, chat))
        return result.rowcount == 1

    def recent(self, chat):
        return self.db.execute('SELECT id,status,attempts,next_attempt,error FROM jobs WHERE chat=? ORDER BY id DESC LIMIT 10', (chat,)).fetchall()

    def owned_file(self, job_id, chat):
        return self.db.execute("SELECT * FROM jobs WHERE id=? AND chat=? AND status IN ('done','deleting')",
                               (job_id, chat)).fetchone()

    def prepare_remote(self, job_id, destination):
        with self.db:
            row = self.db.execute('SELECT name FROM jobs WHERE id=?', (job_id,)).fetchone()
            self.db.execute('UPDATE jobs SET remote_path=COALESCE(remote_path,?) WHERE id=?',
                            (f"{destination}/{job_id}-{row['name']}", job_id))

    def complete(self, job_id, metadata):
        metadata = metadata or {}
        hashes = {key.lower(): value for key, value in metadata.get('Hashes', {}).items()}
        with self.db:
            self.db.execute("UPDATE jobs SET status='done', remote_id=?, remote_md5=? WHERE id=?",
                            (metadata.get('ID'), hashes.get('md5'), job_id))

    def remember_file(self, job_id, metadata):
        hashes = {key.lower(): value for key, value in metadata.get('Hashes', {}).items()}
        with self.db:
            self.db.execute('UPDATE jobs SET remote_id=COALESCE(remote_id,?),remote_md5=COALESCE(remote_md5,?) WHERE id=?',
                            (metadata.get('ID'), hashes.get('md5'), job_id))

    def save_link(self, job_id, url):
        with self.db:
            self.db.execute('UPDATE jobs SET download_url=? WHERE id=?', (url, job_id))

    def files(self, chat, page=0, page_size=5):
        return self.db.execute("SELECT * FROM jobs WHERE chat=? AND status IN ('done','deleting') ORDER BY id DESC LIMIT ? OFFSET ?",
                               (chat, page_size, page*page_size)).fetchall()

    def file_totals(self, chat):
        row = self.db.execute("SELECT count(*) AS count, COALESCE(sum(size),0) AS size FROM jobs WHERE chat=? AND status IN ('done','deleting')", (chat,)).fetchone()
        return row['count'], row['size']

    def job(self, job_id):
        return self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
