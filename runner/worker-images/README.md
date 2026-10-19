Worker containers use official `python:3.12-alpine` and `node:22-alpine` images.
The runner pulls these images automatically when first deploying a project.
Do not allow users to choose their own image or Dockerfile.
