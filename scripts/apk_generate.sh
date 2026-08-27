#!/usr/bin/env bash
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

# ============================================
# Weave Thinker APK 生成脚本
# ============================================
#
# 用法:
#   bash scripts/apk_generate.sh                    # 使用默认服务器地址
#   bash scripts/apk_generate.sh http://x.x.x.x:port  # 指定服务器地址
#
# 配置点:
#   修改下面的 SERVER_URL 变量即可更改默认目标服务器
#
# 前置条件:
#   - ANDROID_HOME 环境变量指向 Android SDK
#   - 或 Android Studio 已安装（脚本会自动查找）
#   - JDK 11+ (通常随 Android Studio 安装)
#
# ============================================

set -e

# =====================
# 配置点: 目标服务器地址
# =====================
SERVER_URL="${1:-<YOUR_SERVER_URL>}"

# ============================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEBVIEW_DIR="$PROJECT_ROOT/webview-app"
APK_DIR="$PROJECT_ROOT/apk"

echo "=========================================="
echo " Weave Thinker APK 构建"
echo "=========================================="
echo " 目标服务器: $SERVER_URL"
echo "=========================================="

# --- 查找 Android SDK ---
find_android_sdk() {
    # 1. 环境变量
    if [ -n "$ANDROID_HOME" ] && [ -d "$ANDROID_HOME" ]; then
        echo "$ANDROID_HOME"
        return
    fi
    if [ -n "$ANDROID_SDK_ROOT" ] && [ -d "$ANDROID_SDK_ROOT" ]; then
        echo "$ANDROID_SDK_ROOT"
        return
    fi

    # 2. 常见安装位置
    local candidates=(
        "$HOME/Android/Sdk"
        "$HOME/android-sdk"
        "/opt/android-sdk"
        "$HOME/Library/Android/sdk"
    )
    for dir in "${candidates[@]}"; do
        if [ -d "$dir/platforms" ]; then
            echo "$dir"
            return
        fi
    done

    # 3. 通过 Android Studio 查找
    local studio_dirs=(
        "$HOME/.local/share/JetBrains"
        "$HOME/.android"
    )
    for base in "${studio_dirs[@]}"; do
        if [ -d "$base" ]; then
            local found
            found=$(find "$base" -name "sdk" -type d -maxdepth 3 2>/dev/null | head -1)
            if [ -n "$found" ] && [ -d "$found/platforms" ]; then
                echo "$found"
                return
            fi
        fi
    done

    return 1
}

# --- 查找 JDK ---
find_java_home() {
    if [ -n "$JAVA_HOME" ] && [ -d "$JAVA_HOME" ]; then
        echo "$JAVA_HOME"
        return
    fi

    # Homebrew JDK (macOS)
    local brew_jdk_dirs=(
        /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
        /opt/homebrew/opt/openjdk@11/libexec/openjdk.jdk/Contents/Home
        /opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home
        /opt/homebrew/opt/java/libexec/openjdk.jdk/Contents/Home
        /usr/local/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
        /usr/local/opt/openjdk@11/libexec/openjdk.jdk/Contents/Home
        /usr/local/opt/openjdk/libexec/openjdk.jdk/Contents/Home
        /usr/local/opt/java/libexec/openjdk.jdk/Contents/Home
    )
    for dir in "${brew_jdk_dirs[@]}"; do
        if [ -f "$dir/bin/java" ]; then
            echo "$dir"
            return
        fi
    done

    # Android Studio 自带 JDK
    local studio_jdk_dirs=(
        "$HOME/.local/share/JetBrains/Toolbox/apps/android-studio/jbr"
        "$HOME/Downloads/android-studio-panda3-linux/android-studio/jbr"
        "$HOME/android-studio/jbr"
        "/opt/android-studio/jbr"
        "/snap/android-studio/current/android-studio/jbr"
    )
    for dir in "${studio_jdk_dirs[@]}"; do
        if [ -f "$dir/bin/java" ]; then
            echo "$dir"
            return
        fi
    done

    # 系统 JDK (PATH 中的 java)
    if command -v java &>/dev/null; then
        local java_path resolved
        java_path=$(which java)
        resolved=$(python3 -c "import os; print(os.path.realpath('$java_path'))" 2>/dev/null)
        if [ -n "$resolved" ]; then
            echo "${resolved%/bin/java}"
            return
        fi
    fi

    return 1
}

# --- 检查前置条件 ---
SDK_DIR=$(find_android_sdk) || {
    echo "❌ 未找到 Android SDK"
    echo ""
    echo "请安装 Android SDK 并设置环境变量:"
    echo "  export ANDROID_HOME=\$HOME/Android/Sdk"
    echo ""
    echo "安装方式:"
    echo "  1. 安装 Android Studio: https://developer.android.com/studio"
    echo "  2. 或使用 sdkmanager 命令行工具:"
    echo "     sdkmanager \"platforms;android-34\" \"build-tools;34.0.0\""
    exit 1
}

JAVA_DIR=$(find_java_home) || {
    echo "❌ 未找到 JDK"
    echo ""
    echo "请安装 JDK 11+ 或设置 JAVA_HOME 环境变量"
    exit 1
}

echo " Android SDK: $SDK_DIR"
echo " JAVA_HOME:   $JAVA_DIR"
echo "=========================================="

# --- 写入 local.properties ---
cat > "$WEBVIEW_DIR/local.properties" <<EOF
sdk.dir=$SDK_DIR
EOF

export JAVA_HOME="$JAVA_DIR"
export ANDROID_HOME="$SDK_DIR"

# --- 生成签名密钥 ---
KEYSTORE="$WEBVIEW_DIR/keystore.jks"
if [ ! -f "$KEYSTORE" ]; then
    echo "▶ 生成签名密钥 ..."
    "$JAVA_DIR/bin/keytool" -genkeypair \
        -alias weavernote \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -keystore "$KEYSTORE" \
        -storepass weavernote -keypass weavernote \
        -dname "CN=Weave Thinker, O=WeaveThinker, L=Beijing, C=CN" \
        2>&1
    echo " 密钥已生成: $KEYSTORE"
fi

# --- 构建 APK ---
cd "$WEBVIEW_DIR"

echo ""
echo "▶ 开始构建 APK ..."
echo ""

./gradlew assembleRelease -PserverUrl="$SERVER_URL" --no-daemon 2>&1

# --- 复制 APK 到输出目录 ---
mkdir -p "$APK_DIR"

APK_SOURCE="$WEBVIEW_DIR/app/build/outputs/apk/release/app-release.apk"
APK_OUTPUT="$APK_DIR/weave-thinker.apk"

if [ -f "$APK_SOURCE" ]; then
    cp "$APK_SOURCE" "$APK_OUTPUT"
    echo ""
    echo "=========================================="
    echo " ✅ APK 生成成功! (已签名)"
    echo " 输出: $APK_OUTPUT"
    echo " 服务器: $SERVER_URL"
    echo "=========================================="
    echo ""
    echo "  安装到设备: adb install $APK_OUTPUT"
else
    echo ""
    echo "❌ APK 文件未找到，构建可能失败"
    echo "   期望路径: $APK_SOURCE"
    ls -la "$WEBVIEW_DIR/app/build/outputs/apk/release/" 2>/dev/null || true
    exit 1
fi
