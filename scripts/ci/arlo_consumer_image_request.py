"""Emit, but never submit, the manual AWS ARLO image-only build request."""
import argparse
import json
import re

CONNECTION = "arn:aws:codestar-connections:us-east-1:807034087062:connection/f4be860d-5308-4694-9f6a-2d6b9c0ffa99"
PINS = {
    "arlo_recipe": ("arlo-3dml", "acd89a0a413774147c3aa12607372d0d9e5b209e"),
    "arlo_source": ("arlo-3dml", "f35a10e864879ac0723fe595fc8dbbe02e09b5f2"),
    "platform_pinned": ("leaf-web-demo", "f20fa02b8c2038eb69a6607792b5cbd5287053c4"),
}


def request(producer_revision):
    if not re.fullmatch(r"[0-9a-f]{40}", producer_revision):
        raise ValueError("Producer revision must be an exact published commit")
    return {
        "projectName": "leaf-studio-native-release",
        "sourceVersion": producer_revision,
        "buildspecOverride": ".codebuild/arlo-consumer-image.yml",
        "timeoutInMinutesOverride": 20,
        "secondarySourcesOverride": [
            {"sourceIdentifier": identifier, "type": "GITHUB",
             "location": f"https://github.com/LEAF-Solar-Design/{repo}.git",
             "gitCloneDepth": 1,
             "auth": {"type": "CODECONNECTIONS", "resource": CONNECTION}}
            for identifier, (repo, _) in PINS.items()
        ],
        "secondarySourcesVersionOverride": [
            {"sourceIdentifier": identifier, "sourceVersion": revision}
            for identifier, (_, revision) in PINS.items()
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-revision", required=True)
    args = parser.parse_args()
    print(json.dumps(request(args.producer_revision), indent=2))
