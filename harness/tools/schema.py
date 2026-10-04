"""Subconjunto explícito de JSON Schema usado por tools; no es un validador completo."""


def validate(value, schema: dict, path: str = 'arguments') -> None:
    kind = schema.get('type')
    kinds = kind if isinstance(kind, list) else [kind]
    checks = {
        'object': lambda: isinstance(value, dict),
        'array': lambda: isinstance(value, list),
        'string': lambda: isinstance(value, str),
        'integer': lambda: isinstance(value, int) and not isinstance(value, bool),
        'number': lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        'boolean': lambda: isinstance(value, bool),
        'null': lambda: value is None,
    }
    if kind is not None and not any(k in checks and checks[k]() for k in kinds):
        raise ValueError(f'{path}: se esperaba {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path}: valor no permitido')
    if isinstance(value, dict):
        for name in schema.get('required', []):
            if name not in value:
                raise ValueError(f'faltan parámetros obligatorios: {path}.{name}')
        properties = schema.get('properties', {})
        for name, item in value.items():
            if name in properties:
                validate(item, properties[name], path + '.' + name)
            elif schema.get('additionalProperties') is False:
                raise ValueError(f'{path}.{name}: parámetro no permitido')
    elif isinstance(value, list):
        if len(value) > schema.get('maxItems', 1000):
            raise ValueError(f'{path}: demasiados elementos')
        for item in value:
            validate(item, schema.get('items', {}), path + '[]')
    elif isinstance(value, str):
        if len(value) > schema.get('maxLength', 1_000_000):
            raise ValueError(f'{path}: texto demasiado largo')
    elif isinstance(value, (float, int)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            raise ValueError(f'{path}: por debajo del mínimo')
        if 'maximum' in schema and value > schema['maximum']:
            raise ValueError(f'{path}: por encima del máximo')
