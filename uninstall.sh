#!/bin/bash
# Removes the Ixzib app and its settings. Add --purge-models to also delete the downloaded speech models (~2 GB).
APP_DIR="${IXZIB_APP_DIR:-$HOME/Applications}"
pkill -f ixzib_core.py 2>/dev/null; pkill -f "widget/electron/Electron.app" 2>/dev/null
rm -rf "$APP_DIR/Ixzib.app" "${IXZIB_HOME:-$HOME/.ixzib}" "$HOME/Library/Logs/Ixzib"
echo "Приложение и настройки удалены."
if [ "${1:-}" = "--purge-models" ]; then
  rm -rf "$HOME/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo" "$HOME/.cache/torch/hub/snakers4_silero-models_master" "$HOME/.cache/torch/hub/checkpoints"
  echo "Модели удалены."
fi
echo "Саму папку проекта можно просто перетащить в корзину."
