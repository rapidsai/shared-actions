# Get DockerHub Token

This composite action generates a DockerHub token and runs a script with it. The token is revoked when the composite action exits.

## Inputs

- `DOCKERHUB_USER` (required): DockerHub username
- `DOCKERHUB_TOKEN` (required): DockerHub password
- `script` (required): Script to run

## Usage

```yaml
jobs:
  example:
    steps:
      - name: Run With DockerHub Token
        uses: rapidsai/shared-actions/dockerhub-script@main
        with:
          DOCKERHUB_USER: ${{ secrets.DOCKERHUB_USERNAME }}
          DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_PASSWORD }}
          script: ci/do_something.sh
        env: #(if needed)
          IMAGE_NAME: rapidsai/base:some-random-tag
          ARCHES: ${{ toJSON(matrix.ARCHES) }}
          #...etc
```
