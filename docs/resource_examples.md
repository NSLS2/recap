## Examples of resource templates

Besides plates and containers, users can also create resource templates that represent files, robots and even people.

### NeXus Hdf5 files

Capture beamline data products with file- and detector-level metadata, plus a nested entry describing the stored dataset.

```python
with client.build_resource_template(name="NeXus File",
                                    type_names=["file", "hdf5", "nexus"]) as template_builder:
    template_builder.add_properties({
        "file_metadata": [
            {"name": "path", "type": "str", "default": ""},
            {"name": "sha256", "type": "str", "default": ""},
            {"name": "created", "type": "datetime", "default": None},
        ],
        "scan": [
            {"name": "instrument", "type": "str", "default": ""},
            {"name": "beamline", "type": "str", "default": ""},
            {"name": "exposure_time", "type": "float", "default": 0.1, "unit": "s"},
        ],
    })

    (template_builder.add_child("entry1", ["group", "nexus_entry"])
        .add_properties({
            "detector": [
                {"name": "distance_mm", "type": "float", "default": 0.0, "unit": "mm"},
                {"name": "pixel_size_um", "type": "float", "default": 75.0, "unit": "um"},
                {"name": "wavelength", "type": "float", "default": 1.0, "unit": "angstrom"},
            ],
            "data": [
                {"name": "shape", "type": "array", "default": [0, 0]},
                {"name": "compression", "type": "enum", "default": "lz4",
                 "metadata": {"choices": ["lz4", "gzip", "bslz4"]}},
            ],
        })
        .close_child())
```

### UR5 Robot

Model the arm, its controller settings and a pluggable end-effector.

```python
with client.build_resource_template(name="UR5 Robot",
                                    type_names=["instrument", "robot", "ur5"]) as template_builder:
    template_builder.add_properties({
        "specs": [
            {"name": "reach_mm", "type": "float", "default": 850.0, "unit": "mm"},
            {"name": "payload_kg", "type": "float", "default": 5.0, "unit": "kg"},
            {"name": "tcp_frame", "type": "array", "default": [0, 0, 0, 0, 0, 0]},
        ],
        "controller": [
            {"name": "ip_address", "type": "str", "default": "192.168.0.10"},
            {"name": "firmware", "type": "str", "default": ""},
            {"name": "safety_mode", "type": "enum", "default": "normal",
             "metadata": {"choices": ["normal", "reduced", "protective_stop"]}},
        ],
    })

    (template_builder.add_child("end_effector", ["instrument", "tool", "gripper"])
        .add_properties({
            "gripper": [
                {"name": "model", "type": "str", "default": ""},
                {"name": "max_width_mm", "type": "float", "default": 85.0, "unit": "mm"},
                {"name": "calibrated", "type": "bool", "default": False},
            ],
            "maintenance": [
                {"name": "last_service", "type": "datetime", "default": None},
                {"name": "cycles_since_service", "type": "int", "default": 0},
            ],
        })
        .close_child())
```

### Scientist

Track personnel involved in experiments with training, contact info and linked notebooks.

```python
with client.build_resource_template(name="Scientist",
                                    type_names=["person", "operator"]) as template_builder:
    template_builder.add_properties({
        "identity": [
            {"name": "full_name", "type": "str", "default": ""},
            {"name": "orcid", "type": "str", "default": ""},
            {"name": "affiliation", "type": "str", "default": ""},
        ],
        "contact": [
            {"name": "email", "type": "str", "default": ""},
            {"name": "phone", "type": "str", "default": ""},
        ],
        "training": [
            {"name": "beamline_certified", "type": "bool", "default": False},
            {"name": "radiation_training_expires", "type": "datetime", "default": None},
        ],
    })

    (template_builder.add_child("notebook", ["record", "lab_notebook"])
        .add_properties({
            "tracking": [
                {"name": "provider", "type": "enum", "default": "eln",
                 "metadata": {"choices": ["eln", "paper", "lims"]}},
                {"name": "identifier", "type": "str", "default": ""},
            ],
        })
        .close_child())
```
