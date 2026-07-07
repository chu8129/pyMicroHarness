#!/bin/bash

NAME="myapp"
ENTRY="__main__.py"

pip install pyinstaller
pyinstaller --onefile --name="$NAME" "$ENTRY" --clean

if [ $? -eq 0 ]; then
    echo "构建成功！文件位于 dist/$NAME.exe"
else
    echo "构建失败。"
    exit 1
fi
