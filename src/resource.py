import os
import sys
import shutil
import filecmp
import requests
from io import BytesIO
from zipfile import ZipFile
from pathlib import Path


def get_base_dir():
    """获取程序运行时的真实根目录（即 exe 所在的目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return Path(__file__).parent.parent


def get_meipass_dir():
    """获取 PyInstaller 运行时的临时解压目录"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    else:
        return Path(__file__).parent.parent


def release_bundled_assets(progress_callback=None):
    """
    将内置的轻量级资源释放到 exe 同级目录。

    不能只在目标目录不存在时才复制：升级 exe 后，旧的 assets 目录会留在原地，
    新版本新增或修改的 pipeline / image 资源就永远落不了地（表现为
    "Cannot find required Pipeline configuration file: .../boot_auto.json"）。
    这里改为逐个文件比对，内容不同就覆盖，保证运行中的资源始终与 exe 一致。
    """
    base_dir = get_base_dir()
    meipass_dir = get_meipass_dir()

    # 我们只关心这两个内置好的目录
    bundled_dirs = [
        os.path.join("assets", "resource", "image"),
        os.path.join("assets", "resource", "pipeline")
    ]

    for rel_path in bundled_dirs:
        # 目标路径（用户可见的 exe 同级目录）
        target_path = os.path.join(base_dir, rel_path)
        # 源路径（隐藏在临时文件夹中的内置文件）
        source_path = os.path.join(meipass_dir, rel_path)

        if not os.path.exists(source_path):
            continue

        if progress_callback:
            progress_callback(f"正在释放内置资源: {os.path.basename(rel_path)}...", -1)

        released = 0
        for root, _, files in os.walk(source_path):
            rel_root = os.path.relpath(root, source_path)
            dest_root = target_path if rel_root == "." else os.path.join(target_path, rel_root)
            os.makedirs(dest_root, exist_ok=True)

            for name in files:
                source_file = os.path.join(root, name)
                dest_file = os.path.join(dest_root, name)
                if os.path.exists(dest_file) and filecmp.cmp(source_file, dest_file, shallow=False):
                    continue
                shutil.copy2(source_file, dest_file)
                released += 1

        if released:
            print(f"✅ 内置资源已同步 {released} 个文件至: {target_path}")

def download_and_extract(url, extract_to_path, progress_callback=None, desc="下载中"):
    """
    带进度回调的下载与解压函数
    progress_callback: 接收两个参数 (text: str, percent: float)
    """
    if progress_callback:
        progress_callback(f"正在连接: {desc}...", 0)

    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()

        # 尝试获取文件总大小 (GitHub 的 archive 接口可能不返回此 header，此时为 0)
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        content = bytearray()

        # 分块读取并实时触发回调
        for data in response.iter_content(chunk_size=8192):
            content.extend(data)
            downloaded += len(data)

            if progress_callback:
                mb_downloaded = downloaded / (1024 * 1024)
                if total_size > 0:
                    percent = (downloaded / total_size) * 100
                    mb_total = total_size / (1024 * 1024)
                    progress_callback(f"{desc}: {mb_downloaded:.2f}MB / {mb_total:.2f}MB", percent)
                else:
                    # 如果没有总大小，只显示已下载大小，进度条传入 -1 表示 "忙碌/未知" 状态
                    progress_callback(f"{desc}: 已下载 {mb_downloaded:.2f}MB", -1)

        if progress_callback:
            progress_callback(f"{desc} 下载完成，正在解压...", 100)

        with ZipFile(BytesIO(content)) as zip_ref:
            zip_ref.extractall(extract_to_path)

    except Exception as e:
        if progress_callback:
            progress_callback(f"❌ {desc} 失败: {str(e)}", 0)
        raise e


def check_and_prepare_resources(progress_callback=None):
    # 0. 首先释放 exe 内部打包的基础文件 (image / pipeline)
    release_bundled_assets(progress_callback)

    base_dir = get_base_dir()
    assets_dir = os.path.join(base_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # 1. 检查 minitouch
    minitouch_target_dir = os.path.join(assets_dir, "minitouch_EvATive7")
    if not os.path.exists(minitouch_target_dir):
        url = "https://github.com/EvATive7/minitouch/releases/latest/download/minitouch.zip"
        temp_dir = os.path.join(assets_dir, "minitouch_temp")

        download_and_extract(url, temp_dir, progress_callback, "minitouch 资源")

        minitouch_src = os.path.join(temp_dir, "minitouch")
        if os.path.exists(minitouch_src):
            shutil.move(minitouch_src, minitouch_target_dir)
            shutil.rmtree(temp_dir, ignore_errors=True)

    # 2. 检查 OCR
    ocr_model_path = os.path.join(assets_dir, "resource", "model", "ocr")
    if not os.path.exists(ocr_model_path):
        url = "https://github.com/Evan-Fantasque/MaaCommonAssets/releases/latest/download/ppocr_v5.zip"
        temp_assets_dir = os.path.join(assets_dir, "maa_assets_temp")

        download_and_extract(url, temp_assets_dir, progress_callback, "OCR 模型")

        extracted_root = os.path.join(temp_assets_dir, "MaaCommonAssets-main")
        src_zh_cn = os.path.join(extracted_root, "OCR", "ppocr_v5", "zh_cn")
        src_zh_cn_server = os.path.join(extracted_root, "OCR", "ppocr_v5", "zh_cn-server")

        if progress_callback:
            progress_callback("正在整理 OCR 模型文件...", 100)

        if os.path.exists(src_zh_cn) and os.path.exists(src_zh_cn_server):
            os.makedirs(ocr_model_path, exist_ok=True)
            shutil.copytree(src_zh_cn, ocr_model_path, ignore=shutil.ignore_patterns("README.md"), dirs_exist_ok=True)
            target_zh_cn_server = os.path.join(ocr_model_path, "ppocr_v5", "zh_cn-server")
            shutil.copytree(src_zh_cn_server, target_zh_cn_server, dirs_exist_ok=True)

        shutil.rmtree(temp_assets_dir, ignore_errors=True)

    if progress_callback:
        progress_callback("✅ 所有资源准备就绪！", 100)