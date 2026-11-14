#!/usr/bin/env sh
# Install the Lambda + Pi VS Code extension without a build step.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
dst="$HOME/.vscode/extensions/lambda-language-0.1.0"
mkdir -p "$dst"
cp -R "$here/vscode/." "$dst/"
echo "Installed Lambda + Pi highlighting to:"
echo "  $dst"
echo "Fully restart VS Code, then open a .lambda or .pi file."
