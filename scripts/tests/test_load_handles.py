"""load_handles decides who gets tracked at all - a parsing bug here
means a handle silently never gets checked, with no error anywhere."""
from pathlib import Path

from check_posts import load_handles


def test_strips_at_sign_and_whitespace(tmp_path):
    csv_path = tmp_path / "handles.csv"
    csv_path.write_text("@torch_boy\n  angiechack  \nbry.trieu\n")

    assert load_handles(csv_path) == ["torch_boy", "angiechack", "bry.trieu"]


def test_skips_blank_lines(tmp_path):
    csv_path = tmp_path / "handles.csv"
    csv_path.write_text("torch_boy\n\n\nangiechack\n")

    assert load_handles(csv_path) == ["torch_boy", "angiechack"]


def test_skips_a_header_row_case_insensitively(tmp_path):
    csv_path = tmp_path / "handles.csv"
    csv_path.write_text("Handle\ntorch_boy\n")

    assert load_handles(csv_path) == ["torch_boy"]


def test_missing_file_returns_empty_list_not_an_error(tmp_path):
    assert load_handles(tmp_path / "does_not_exist.csv") == []


def test_preserves_input_order(tmp_path):
    csv_path = tmp_path / "handles.csv"
    csv_path.write_text("z_handle\na_handle\nm_handle\n")

    assert load_handles(csv_path) == ["z_handle", "a_handle", "m_handle"]
