import re

PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


class TemplateError(ValueError):
    pass


def placeholders(body):
    return list(dict.fromkeys(PLACEHOLDER.findall(body)))


def validate_template(body, allowed):
    invalid = sorted(set(placeholders(body)) - set(allowed))
    if invalid:
        raise TemplateError(f"Unknown placeholders: {', '.join(invalid)}")
    return placeholders(body)


def render_message(body, row, policy="empty", fallback=""):
    missing = []
    def replace(match):
        key = match.group(1)
        value = row.get(key)
        if value is None or str(value).strip() == "":
            missing.append(key)
            return fallback if policy == "fallback" else ""
        return str(value)
    rendered = PLACEHOLDER.sub(replace, body)
    rendered = rendered.replace("{{", "{").replace("}}", "}")
    if missing and policy == "skip":
        return None, missing
    return rendered, missing
