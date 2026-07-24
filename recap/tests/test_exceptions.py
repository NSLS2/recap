def test_recap_connection_error_importable():
    from recap.exceptions import RecapConnectionError

    err = RecapConnectionError("http://localhost:8000", 404)
    assert "http://localhost:8000" in str(err)
    assert "404" in str(err)


def test_recap_connection_error_is_exception():
    from recap.exceptions import RecapConnectionError

    assert issubclass(RecapConnectionError, Exception)
