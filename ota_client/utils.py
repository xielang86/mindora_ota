import hashlib

def calc_sha256(path):

    sha = hashlib.sha256()

    with open(path, "rb") as f:

        while True:

            data = f.read(
                1024 * 1024
            )

            if not data:
                break

            sha.update(data)

    return sha.hexdigest()


def verify_sha256(
    path,
    expected
):

    return (
        calc_sha256(path)
        ==
        expected.lower()
    )