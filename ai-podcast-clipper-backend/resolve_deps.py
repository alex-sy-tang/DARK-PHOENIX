import modal

image = (
    modal.Image.from_registry("nvidia/cuda:12.4.0-devel-ubuntu22.04")
    .add_python("3.11")
    .pip_install("pip-tools")
    .copy_local_file("requirements.in", "/requirements.in")
    .run_commands(
        "pip-compile /requirements.in --output-file /requirements.txt --resolver=backtracking 2>&1"
    )
)

app = modal.App("resolve-deps", image=image)

@app.function()
def get_lockfile():
    with open("/requirements.txt") as f:
        return f.read()

@app.local_entrypoint()
def main():
    content = get_lockfile.remote()
    with open("requirements.lock", "w") as f:
        f.write(content)
    print("Lockfile written to requirements.lock")
    print(content)
