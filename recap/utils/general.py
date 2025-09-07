from slugify import slugify

def generate_uppercase_alphabets(n: int) -> list:
    if n < 1:
        raise ValueError("The number must be a positive integer.")

    alphabets = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def get_letter(num):
        result = []
        while num > 0:
            num, remainder = divmod(num - 1, 26)
            result.append(alphabets[remainder])
        return "".join(reversed(result))

    return [get_letter(i) for i in range(1, n + 1)]

def make_slug(value: str) -> str:
    """
    Generate a slug that is always a valid Python identifier.
    """
    regex_pattern = r'[^a-z0-9_]+'  # allow only lowercase letters, digits, underscores
    slug = slugify(
        value,
        lowercase=True,
        separator="_",
        regex_pattern=regex_pattern,
    )
    # Ensure it doesn't start with a digit (prepend underscore if so)
    if slug and slug[0].isdigit():
        slug = f"_{slug}"
    return slug
