from names import to_snake


def test_leading_and_trailing_acronyms() -> None:
    assert to_snake("HTTPServerURL") == "http_server_url"


def test_internal_acronym_boundary() -> None:
    assert to_snake("parseXMLDocument") == "parse_xml_document"


def test_digits_next_to_acronyms() -> None:
    assert to_snake("JSON2XMLParser") == "json2_xml_parser"


def test_existing_separators_stay_single() -> None:
    assert to_snake("already-snake name") == "already_snake_name"
