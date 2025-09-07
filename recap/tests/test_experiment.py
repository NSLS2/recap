def test_experiment(db_session):
    from recap.models.process import ProcessTemplate

    experiment_type = ProcessTemplate(name="Test Experiment")
    db_session.add(experiment_type)
