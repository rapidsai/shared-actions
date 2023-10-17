# Get DockerHub Token

This composite action generates a DockerHub token and runs a script with it.

## Inputs

- `DOCKERHUB_USER` (required): DockerHub username
- `DOCKERHUB_TOKEN` (required): DockerHub password
- `script` (required): Script to run

## Usage

```yaml
jobs:
  example:
    steps:
      - name: Get DockerHub Token
        id: dockerhub_token
        uses: rapidsai/shared-actions/dockerhub-script@branch-23.12
        with:
          DOCKERHUB_USER: ${{ secrets.DOCKERHUB_USERNAME }}
          DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_PASSWORD }}
          script: ci/do_something.sh
        env: (if needed)
          IMAGE_NAME: rapidsai/base:some-random-tag
          ARCHES: ${{ toJSON(matrix.ARCHES) }}
          ...etc
```
