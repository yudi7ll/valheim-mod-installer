#!/bin/sh
set -e

MOD_DIR="./mods"
CACHE="./cache"
MODS_FILE="./mods.txt"
API_BASE="https://thunderstore.io/api/experimental/package"

[ ! -f "$MODS_FILE" ] && touch "$MODS_FILE"

mkdir -p "$MOD_DIR" "$CACHE"

install_mod() {
  author="$1"
  name="$2"
  is_deps="$3"

  key="$author-$name"
  [ -z "$key" ] && return
  [ -d "$CACHE/$key" ] && return

  meta=$(curl -s "$API_BASE/$author/$name/")
  version=$(echo "$meta" | jq -r '.latest.version_number')
  download_url=$(echo "$meta" | jq -r '.latest.download_url')
  dependencies=$(echo "$meta" | jq -r '.latest.dependencies[]?')
  if [ $is_deps -eq 1 ]; then
    printf "\033[33mInstalling dependencies $key-$version\033[0m\n"
  else
    printf "\033[32mInstalling $key-$version\033[0m\n"
  fi

  mkdir -p "$CACHE/$key"
  mkdir -p "$MOD_DIR/$key"
  
  filepath="$CACHE/$key-$version.zip"
  modpath="$MOD_DIR/$key"

  wget -q "$download_url" -O "$filepath"
  bsdtar -xf "$filepath" -C "$modpath"

  echo "$dependencies" | while read dep; do
    if [ -z "$dep" ]; then
      continue
    fi
    a=$(echo "$dep" | cut -d- -f1)
    n=$(echo "$dep" | cut -d- -f2)
    install_mod "$a" "$n" 1
  done
}

rm -rf "$MOD_DIR"/* "$CACHE"

while IFS=/ read -r author name; do
  install_mod "$author" "$name" 0
done < "$MODS_FILE"
