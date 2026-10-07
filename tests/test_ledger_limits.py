import json
import sqlite3
from contextlib import contextmanager

import pytest
import agent.archive
import agent.conversation_store as store
from agent.ledger_payload import safe_payload


def test_oversized_payload_under_real_sqlite_limit(monkeypatch, tmp_path, caplog):
    """Reproduce SQLITE_TOOBIG, then verify guarded writes on the same limit."""
    monkeypatch.setattr(agent.archive, 'records_db_path', lambda: str(tmp_path / 'records.db'))
    monkeypatch.setattr(agent.archive, 'archived_media_dir', lambda: str(tmp_path / 'media'))
    original = store.connect

    @contextmanager
    def limited():
        """Lower SQLite's actual limit after schema initialization."""
        with original() as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8192)
            yield conn

    with limited() as conn:
        with pytest.raises(sqlite3.DataError, match='too big'):
            conn.execute("INSERT INTO conversation_entries(entry_id,role,content,created_at) VALUES(?,?,?,?)",
                         ('bad', 'tool', '中' * 10000, 'now'))
    monkeypatch.setattr(store, 'connect', limited)
    caplog.set_level('INFO')
    saved = store.save_conversation_entry('tool', {'name': 'view_output', 'result': '中' * 100000},
                                           metadata={'context': '文' * 100000})
    entry = store.read_conversation_entry(saved['entry_id'])
    assert saved['inserted']
    assert len(entry['content'].encode()) <= 2048
    assert 'field_bytes' in caplog.text
    assert 'oversized leaf' in caplog.text
    assert entry['metadata']


def test_media_binary_embedding_are_bounded():
    """Inline image data, binary buffers and long vectors stay out of the ledger."""
    data = safe_payload({'image': 'data:image/png;base64,' + 'A' * 100000,
                         'blob': b'abc' * 100000, 'embedding': list(range(100000))})
    encoded = json.dumps(data)
    assert 'A' * 4096 not in encoded
    assert data['blob']['bytes'] == 300000
    assert len(data['embedding']) <= 65


def test_spill_reader_bounds_single_line_and_matches_exact_id(monkeypatch, tmp_path):
    """A giant JSON line returns a bounded preview without recursive serialization."""
    import agent.outputs as outputs
    monkeypatch.setattr(outputs, 'SPILL_DIR', str(tmp_path))
    (tmp_path / '20261007_L20.txt').write_text('wrong', encoding='utf-8')
    (tmp_path / '20261007_L2.txt').write_text('中' * 100000, encoding='utf-8')
    result = outputs.view('L2')
    assert len(result['data']['text']) == 2000
    assert result['data']['truncated']
    assert len(list(tmp_path.iterdir())) == 2


def test_legacy_huge_rows_are_bounded_in_sql(monkeypatch, tmp_path):
    """Old oversized rows stay intact while tool/search reads return previews."""
    monkeypatch.setattr(agent.archive, 'records_db_path', lambda: str(tmp_path / 'records.db'))
    monkeypatch.setattr(agent.archive, 'archived_media_dir', lambda: str(tmp_path / 'media'))
    with store.connect() as conn:
        conn.execute("INSERT INTO conversation_entries(entry_id,role,content,created_at) VALUES(?,?,?,?)",
                     ('legacy', 'tool', '旧' * 100000, 'now'))
    result = store.search_conversations(role='tool', max_content_chars=100)
    assert len(result[0]['content']) == 101
    assert result[0]['truncated']
    preview = store.read_conversation_entry('legacy', max_content_chars=4000)
    assert len(preview['content']) == 4000
    assert preview['content_chars'] == 100000
    assert preview['truncated']


def test_spill_pagination_preserves_original_id(monkeypatch, tmp_path):
    """Read successive pages of a single long line without creating new spills."""
    import agent.outputs as outputs
    monkeypatch.setattr(outputs, 'SPILL_DIR', str(tmp_path))
    original = '甲' * 2000 + '乙' * 2000 + '尾'
    (tmp_path / '20261007_L7.txt').write_text(original, encoding='utf-8')
    first = outputs.view('L7')['data']
    second = outputs.view('L7', char_offset=first['next_char_offset'])['data']
    third = outputs.view('L7', char_offset=second['next_char_offset'])['data']
    assert first['text'] + second['text'] + third['text'] == original
    assert not third['truncated']
    assert len(list(tmp_path.iterdir())) == 1
