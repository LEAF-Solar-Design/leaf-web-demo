"""Author read-only action catalog searches over host-bound, versioned metadata."""
import re


def matches(description):
    prompt = description.casefold()
    return bool(re.search(r"\bactions?\b", prompt) and
                re.search(r"\b(?:which|what|show|list|discover|find|identify)\b", prompt))


def build_body(description):
    prompt = description.casefold()
    # A purpose immediately after "action" specifies what the operation must do.
    purpose = re.search(r"\bactions?\s+(?:can|that can|for|to)\s+(.+)", prompt)
    query = purpose[1] if purpose else prompt
    return _BODY.replace("__QUERY__", repr(query))


_BODY = r"""
import re

def terms(value):
    ignored = {"a", "an", "the", "my", "our", "i", "we", "which", "what", "show", "list",
               "discover", "find", "identify", "me", "all", "each", "of", "from", "in", "on",
               "is", "are", "available", "connected", "integration", "integrations", "project",
               "action", "actions", "drawing", "checker", "can", "could", "call", "before",
               "choose", "one", "pinned", "reference", "references", "for", "to", "that"}
    result = set()
    for word in re.findall(r"[a-z0-9]+", value.casefold()):
        if word in ignored:
            continue
        if len(word) > 5 and word.endswith("ing"):
            word = word[:-3]
        elif len(word) > 4 and word.endswith("s"):
            word = word[:-1]
        result.add(word.removesuffix("e") if len(word) > 3 else word)
    return result


def run(intake, params):
    catalog = intake.get("project_action_catalog")
    if catalog is None:
        raise ValueError("This report needs the current project's turn-bound action catalog.")
    query = terms(__QUERY__)
    actions = []
    for action in catalog["actions"]:
        searchable = " ".join([action["title"], action["connection_title"], action["connection_id"],
                               action["operation_ref"]["id"], action["connector_ref"]["id"]])
        if query <= terms(searchable):
            actions.append(action)
    return ({"actions": actions, "actions_scanned": len(catalog["actions"]),
             "pages_read": catalog["pages_read"]}, None)
"""
