def test_experiment(db_session):
    from recap.models.experiment import ExperimentType

    experiment_type = ExperimentType(name="Test Experiment")
