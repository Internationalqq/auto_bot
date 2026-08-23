from autobot.pdf_estimate_adapter import _section_for_position, _section_markers_from_words


def _line(top: float, text: str) -> list[dict[str, object]]:
    return [
        {"text": token, "left": index * 80, "top": top, "width": 60, "height": 20}
        for index, token in enumerate(text.split())
    ]


def test_section_detection_uses_start_and_following_total_marker():
    words = [
        *_line(100, "Раздел 1. Земляные работы"),
        *_line(220, "Всего по разделу 1 Земляные работы 1 250 000,00"),
        # OCR may miss the next section header; its final line still defines the range.
        *_line(460, "Всего по разделу 2 Установка бортовых камней 800 000,00"),
    ]

    starts, ends = _section_markers_from_words(words)

    assert _section_for_position(160, starts, ends) == "Раздел 1. Земляные работы"
    assert _section_for_position(340, starts, ends) == "Раздел 2. Установка бортовых камней"
