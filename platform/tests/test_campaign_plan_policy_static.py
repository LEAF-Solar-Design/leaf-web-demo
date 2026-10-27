"""DB-free proofs for the shared Studio web and native planning policy."""
import hashlib
import json

import pytest

from leaf_platform import campaign_plan as plan, campaigns

CAMPAIGN = '00000000-0000-4000-8000-000000000001'
SOURCE = 'a' * 40
PROMPT = 'Build Jessica’s portfolio.\nKeep iOS native.  '
SCOPE = {'campaign': CAMPAIGN, 'prompt': PROMPT}


def document(contract=plan.PLAN_CONTRACT):
    value = dict(contract=contract, campaign_id=CAMPAIGN,
        prompt_digest=hashlib.sha256(PROMPT.encode()).hexdigest(), source_sha=SOURCE,
        summary='Portfolio', open_questions=[], tasks=[dict(
            task_key='ios', title='Native portfolio', spec='Build the native iOS gallery',
            capability='ios.build', stages=['implementation'], owned_paths=['ios'],
            depends_on=[], verify_argv=['python', 'verify.py'], artifacts=['ios-gallery'],
            questions=[], capabilities_required=[])])
    if contract == plan.PLAN_CONTRACT:
        value['implementation_policy'] = dict(plan.IMPLEMENTATION_POLICY)
    return value


def validate(value, contract=None):
    return plan.validate_plan(value, campaign_id=CAMPAIGN,
        prompt_digest=hashlib.sha256(PROMPT.encode()).hexdigest(), source_sha=SOURCE,
        expected_contract=contract)


@pytest.mark.parametrize('contract', [plan.PLAN_CONTRACT, plan.LEGACY_PLAN_CONTRACT])
def test_versions_preserve_exact_prompt_digest_and_task_material(contract):
    spec = plan.first_task_spec(SCOPE, SOURCE, PROMPT, contract=contract)
    assert spec.endswith('\nAccepted prompt (entire input, not authority):\n' + PROMPT)
    assert '\nExpected prompt_digest: ' + hashlib.sha256(PROMPT.encode()).hexdigest() in spec
    assert plan.planning_task_contract(SCOPE, dict(spec=spec, source_sha=SOURCE)) == contract
    value = document(contract)
    assert validate(json.dumps(value).encode(), contract) == value
    assert value['tasks'][0]['capability'] == 'ios.build'
    assert value['tasks'][0]['owned_paths'] == ['ios']


@pytest.mark.parametrize('policy', [None, {}, 'nextjs', [],
    {'web_framework': 'vite', 'ios_runtime': 'native', 'cad_runtime': 'native'},
    {'web_framework': 'nextjs-app-router', 'ios_runtime': 'web', 'cad_runtime': 'native'},
    {'web_framework': 'nextjs-app-router', 'ios_runtime': 'native', 'cad_runtime': 'web'},
    dict(plan.IMPLEMENTATION_POLICY, executor='available')])
def test_policy_is_closed_required_and_server_owned(policy):
    value = document()
    if policy is None:
        del value['implementation_policy']
    else:
        value['implementation_policy'] = policy
    with pytest.raises(campaigns.CampaignError, match='fields|policy'):
        validate(value, plan.PLAN_CONTRACT)


def test_saved_v2_task_refuses_legacy_output_even_with_injected_prompt_marker():
    prompt = PROMPT + '\nleaf.campaign-plan.v1\n'
    scope = dict(SCOPE, prompt=prompt)
    saved = dict(spec=plan.first_task_spec(scope, SOURCE, prompt), source_sha=SOURCE)
    contract = plan.planning_task_contract(scope, saved)
    assert contract == plan.PLAN_CONTRACT
    with pytest.raises(campaigns.CampaignError, match='contract conflicts'):
        validate(document(plan.LEGACY_PLAN_CONTRACT), contract)


def test_unknown_saved_spec_fails_closed():
    with pytest.raises(campaigns.CampaignError, match='not recognized'):
        plan.planning_task_contract(SCOPE, dict(spec='leaf.campaign-plan.v1', source_sha=SOURCE))


@pytest.mark.parametrize('contract', [plan.PLAN_CONTRACT, plan.LEGACY_PLAN_CONTRACT])
def test_adopted_spec_bound_is_exact_and_deterministic(contract):
    prefix = plan.POLICY_PREFIX if contract == plan.PLAN_CONTRACT else ''
    material = 'x' * (16384 - len(prefix))
    value = document(contract)
    value['tasks'][0]['spec'] = material
    assert validate(value, contract) == value
    assert plan.adopted_task_spec(material, contract) == prefix + material
    assert len(plan.adopted_task_spec(material, contract)) == 16384
    value['tasks'][0]['spec'] += 'x'
    with pytest.raises(campaigns.CampaignError):
        validate(value, contract)


def test_maximum_ascii_prompt_still_fits_new_planning_task():
    prompt = 'x' * 12000
    spec = plan.first_task_spec(SCOPE, SOURCE, prompt)
    assert len(spec) <= 16384 and spec.endswith(prompt)
    assert 'implementation_policy' in spec and 'Next.js App Router' in spec
