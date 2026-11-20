import json
from pathlib import Path

from infra.render import render


def test_generated_templates_current_and_direct_api_size():
    for name, template in render().items():
        text = Path("infra", name).read_text()
        assert json.loads(text) == template
        assert len(text.encode()) <= 51200


def test_default_does_not_start_compute_and_no_nat():
    template = render()["service.json"]
    assert template["Parameters"]["DesiredCount"]["Default"] == 0
    assert all(value["Type"] != "AWS::EC2::NatGateway" for value in template["Resources"].values())
    assert template["Resources"]["State"]["DeletionPolicy"] == "Retain"
