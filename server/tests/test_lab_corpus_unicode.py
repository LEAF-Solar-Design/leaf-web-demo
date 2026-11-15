import gzip
import pytest
from project_reference_materials import validate_corpus, InvalidReference

@pytest.mark.parametrize('raw', [
    '{"x":1}\n'.encode('utf-16'),
    b'{"x":"\xff"}\n',
    rb'{"x":"\ud800"}' + b'\n',
    rb'{"x":["\udfff"]}' + b'\n',
    rb'{"\ud800":1}' + b'\n',
])
def test_invalid_unicode_rows_refuse(raw):
    with pytest.raises(InvalidReference):
        validate_corpus(gzip.compress(raw))

@pytest.mark.parametrize('raw', [
    rb'{"x":"\ud83d\ude00"}' + b'\n',
    rb'{"x":"\\ud800"}' + b'\n',
    '{"x":"caf\u00e9"}\n'.encode('utf-8'),
    b'{"x":1}\n',
])
def test_valid_unicode_and_literal_escape_rows_retain_support(raw):
    validate_corpus(gzip.compress(raw))
