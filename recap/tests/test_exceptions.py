def test_recap_connection_error_importable():
    from recap.exceptions import RecapConnectionError

    err = RecapConnectionError(
        "Connection failed", url="http://localhost:8000", status_code=404
    )
    assert err.message == "Connection failed"
    assert err.url == "http://localhost:8000"
    assert err.status_code == 404
    assert str(err) == "Connection failed; HTTP 404"


def test_recap_connection_error_is_exception():
    from recap.exceptions import RecapConnectionError

    assert issubclass(RecapConnectionError, Exception)
