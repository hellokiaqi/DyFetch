import cv2
import argparse
from pathlib import Path
from tqdm import tqdm


VIDEO_EXTENSIONS = {
    ".mp4", ".avi", ".mov", ".mkv", ".flv",
    ".wmv", ".webm", ".mpeg", ".mpg", ".m4v"
}


def find_videos(input_dir: Path):
    return [
        p for p in input_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]


def make_unique_dir(output_root: Path, folder_name: str):
    target_dir = output_root / folder_name

    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    index = 1
    while True:
        new_dir = output_root / f"{folder_name}_{index}"
        if not new_dir.exists():
            new_dir.mkdir(parents=True, exist_ok=True)
            return new_dir
        index += 1


def imwrite_unicode(output_file: Path, frame, image_format: str):
    """
    兼容 Windows 中文路径的图片保存函数。
    不用 cv2.imwrite，而是用 cv2.imencode + tofile。
    """

    image_format = image_format.lower()

    if image_format == "jpg":
        ext = ".jpg"
        params = [cv2.IMWRITE_JPEG_QUALITY, 95]
    elif image_format == "png":
        ext = ".png"
        params = [cv2.IMWRITE_PNG_COMPRESSION, 3]
    else:
        raise ValueError(f"不支持的图片格式：{image_format}")

    success, encoded_img = cv2.imencode(ext, frame, params)

    if not success:
        return False

    try:
        encoded_img.tofile(str(output_file))
        return output_file.exists() and output_file.stat().st_size > 0
    except Exception as e:
        print(f"[写入失败] {output_file}")
        print(e)
        return False


def extract_frames_by_frame(
    video_path: Path,
    output_root: Path,
    image_format: str = "jpg",
    frame_interval: int = 1
):
    if frame_interval <= 0:
        raise ValueError("frame_interval 必须大于 0")

    cap = cv2.VideoCapture(str(video_path))

    if not cap.isOpened():
        print(f"[跳过] 无法打开视频：{video_path}")
        return

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    print(f"\n视频：{video_path.name}")
    print(f"FPS：{fps}")
    print(f"视频总帧数：{total_frames}")

    output_dir = make_unique_dir(output_root, video_path.stem)

    frame_index = 0
    saved_count = 0
    failed_count = 0

    pbar_total = total_frames if total_frames > 0 else None

    with tqdm(total=pbar_total, desc=f"处理 {video_path.name}") as pbar:
        while True:
            success, frame = cap.read()

            if not success:
                break

            if frame_index % frame_interval == 0:
                output_file = output_dir / f"frame_{frame_index:08d}.{image_format}"

                write_success = imwrite_unicode(
                    output_file=output_file,
                    frame=frame,
                    image_format=image_format
                )

                if write_success:
                    saved_count += 1
                else:
                    failed_count += 1

            frame_index += 1
            pbar.update(1)

    cap.release()

    print(f"实际读取帧数：{frame_index}")
    print(f"成功保存截图数量：{saved_count}")
    print(f"保存失败数量：{failed_count}")
    print(f"输出目录：{output_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="遍历文件夹中的视频，并按帧截图"
    )

    parser.add_argument(
        "input_dir",
        type=str,
        help="输入视频文件夹路径"
    )

    parser.add_argument(
        "output_dir",
        type=str,
        help="截图输出文件夹路径"
    )

    parser.add_argument(
        "--format",
        type=str,
        default="jpg",
        choices=["jpg", "png"],
        help="截图格式，默认 jpg"
    )

    parser.add_argument(
        "--frame_interval",
        type=int,
        default=1,
        help="每隔多少帧截图一次，默认 1，即每帧都截"
    )

    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    if not input_dir.exists():
        raise FileNotFoundError(f"输入文件夹不存在：{input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    videos = find_videos(input_dir)

    if not videos:
        print("没有找到视频文件。")
        return

    print(f"共找到 {len(videos)} 个视频文件。")

    for video_path in videos:
        extract_frames_by_frame(
            video_path=video_path,
            output_root=output_dir,
            image_format=args.format,
            frame_interval=args.frame_interval
        )

    print("\n全部处理完成。")


if __name__ == "__main__":
    main()