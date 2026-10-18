-- 004_wiki_and_analysis.sql: Analysis cards, comparative questions, and Hypertext Wiki system

CREATE TABLE IF NOT EXISTS analysis_cards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    user_id    INTEGER,
    summary    TEXT    NOT NULL DEFAULT '',
    created_at TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS analysis_questions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id           INTEGER NOT NULL,
    question          TEXT    NOT NULL,
    answer            TEXT    DEFAULT '',
    source_mode       TEXT    DEFAULT '',
    source_message_id INTEGER,
    created_at        TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (card_id) REFERENCES analysis_cards(id)
);

CREATE TABLE IF NOT EXISTS wiki_pages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id               TEXT NOT NULL,
    title               TEXT NOT NULL,
    slug                TEXT NOT NULL,
    content             TEXT NOT NULL,
    tags                TEXT NOT NULL DEFAULT '[]',
    current_revision_id INTEGER DEFAULT 0,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    UNIQUE(kb_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_kb_slug ON wiki_pages(kb_id, slug);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_kb_title ON wiki_pages(kb_id, title);

CREATE TABLE IF NOT EXISTS wiki_revisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id      INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    revision_num INTEGER NOT NULL,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    delta        TEXT,
    author_id    TEXT,
    comment      TEXT NOT NULL,
    sha256_hash  TEXT NOT NULL,
    created_at   REAL NOT NULL,
    UNIQUE(page_id, revision_num)
);
CREATE INDEX IF NOT EXISTS idx_wiki_revisions_page ON wiki_revisions(page_id, revision_num);

CREATE TABLE IF NOT EXISTS wiki_links (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    target_title   TEXT NOT NULL,
    target_page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wiki_links_source ON wiki_links(source_page_id);
CREATE INDEX IF NOT EXISTS idx_wiki_links_target_title ON wiki_links(target_title);
CREATE INDEX IF NOT EXISTS idx_wiki_links_target_page ON wiki_links(target_page_id);
