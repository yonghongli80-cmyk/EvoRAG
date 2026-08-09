"""Output auditing must mask every real secret without substring false positives."""

from rag.guard import check_output


def test_check_output_masks_every_secret_token_completely():
    first = "sk-abcdefghijk1"
    second = "sk-zyxwvutsrqp2"

    result = check_output(f"keys: {first}, {second}")

    assert result.filtered is True
    assert first not in result.text
    assert second not in result.text
    assert "abcdefghijk1" not in result.text
    assert "zyxwvutsrqp2" not in result.text
    assert result.text.count("[已过滤]") == 2


def test_check_output_does_not_mask_normal_word_containing_sk_hyphen():
    text = "The risk-analysis report is ready."

    result = check_output(text)

    assert result.filtered is False
    assert result.text == text
