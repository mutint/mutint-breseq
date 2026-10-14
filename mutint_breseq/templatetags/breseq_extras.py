from django import template

register = template.Library()


@register.filter
def dict_get(mapping, key):
    """`{{ counts|dict_get:choice }}` -- a lookup by a variable key, which the template
    language cannot otherwise spell."""
    try:
        return mapping.get(key, "")
    except AttributeError:
        return ""
