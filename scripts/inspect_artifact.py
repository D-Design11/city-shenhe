#!/usr/bin/env python3
"""Read-only final PNG preflight: 1:1 mm, 100 PPI, binary results."""
import argparse
import hashlib
import json
import math
import struct
import sys
import zlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path

CATEGORIES = ("logo", "slogan", "size")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

def positive(value):
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("尺寸必须为有限正数") from exc
    if not number.is_finite() or number <= 0 or not math.isfinite(float(number)):
        raise argparse.ArgumentTypeError("尺寸必须为有限正数")
    return number


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def png_metadata(path):
    """Read IHDR/pHYs, checking bounds/metadata CRCs, without decoding pixels."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if handle.read(8) != PNG_SIGNATURE:
            return {"format": "unsupported", "reason": "助手仅解析 PNG；请用其他工具读取实际尺寸"}
        dimensions, density = None, None
        saw_data = False
        while handle.tell() < size:
            header = handle.read(8)
            if len(header) != 8:
                raise ValueError("PNG 分块头不完整")
            length, kind = struct.unpack(">I4s", header)
            if handle.tell() + length + 4 > size:
                raise ValueError("PNG 分块越过文件末尾")
            if dimensions is None and kind != b"IHDR":
                raise ValueError("PNG 首块不是 IHDR")
            if kind in (b"IHDR", b"pHYs", b"IEND"):
                expected_length = {b"IHDR": 13, b"pHYs": 9, b"IEND": 0}[kind]
                if length != expected_length:
                    raise ValueError("PNG 元数据分块长度错误")
                data = handle.read(length)
                crc = struct.unpack(">I", handle.read(4))[0]
                if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
                    raise ValueError("PNG 元数据 CRC 不匹配")
                if kind == b"IHDR":
                    if dimensions is not None:
                        raise ValueError("PNG 存在重复 IHDR")
                    width, height = struct.unpack(">II", data[:8])
                    if not width or not height:
                        raise ValueError("PNG 宽高不是正整数")
                    dimensions = {"width_px": width, "height_px": height}
                elif kind == b"pHYs":
                    if density is not None or saw_data:
                        raise ValueError("PNG pHYs 重复或位于图像数据之后")
                    x_ppm, y_ppm, unit = struct.unpack(">IIB", data)
                    density = {"x_ppm": x_ppm, "y_ppm": y_ppm, "unit": unit}
                    if unit == 1:
                        density.update(x_dpi=x_ppm * 0.0254, y_dpi=y_ppm * 0.0254)
                elif kind == b"IEND":
                    if not saw_data:
                        raise ValueError("PNG 缺少图像数据分块")
                    return {"format": "png", **dimensions, "density": density,
                            "validation_scope": "仅元数据与结构边界；未解码图像像素"}
            else:
                saw_data = saw_data or kind == b"IDAT"
                handle.seek(length + 4, 1)
        raise ValueError("PNG 缺少 IEND")


def target_pixels(width, height):
    with localcontext() as context:
        context.prec = max(28, *(max(0, n.adjusted()) + len(n.as_tuple().digits) + 20 for n in (width, height)))
        pixels = [int((n / Decimal("25.4") * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                  for n in (width, height)]
    if min(pixels) < 1:
        raise ValueError("换算后的成品宽高至少为1像素，不自动改大尺寸")
    return pixels


def merge_results(results):
    return "passed" if results and all(result == "passed" for result in results) else "failed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--png", type=Path, help="最终PNG图片")
    source.add_argument("--artifact", type=Path, help="最终PNG图片，兼容单文件输入")
    parser.add_argument("--role", choices=("final", "preview", "screenshot", "unknown"), default="unknown")
    parser.add_argument("--width", type=positive, help="最终宽度mm")
    parser.add_argument("--height", type=positive, help="最终高度mm")
    parser.add_argument("--unit", choices=("mm",), default="mm")
    parser.add_argument("--quality-note", action="append", default=[], help="素材清晰度说明；不参与通过判定，不阻止导出")
    parser.add_argument("--out", type=Path, help="可选内部JSON报告，不覆盖已有文件；不是额外成品交付物")
    args = parser.parse_args(argv)
    if (args.width is None) != (args.height is None):
        parser.error("最终宽高mm须同时提供，不能猜测缺少的一边")
    path = (args.png or args.artifact).resolve()
    if args.out and args.out.resolve() == path:
        parser.error("报告不能覆盖审核原文件")
    try:
        pixels = target_pixels(args.width, args.height) if args.width is not None else None
        artifacts = []
        try:
            digest = sha256_file(path)
            artifacts.append({"path": str(path), "format": "png", "sha256": digest, "role": args.role})
            png = png_metadata(path)
            if png.get("format") != "png":
                raise ValueError("文件内容与最终PNG交付格式不符")
        except (OSError, ValueError, ArithmeticError) as exc:
            png = {"metadata_error": str(exc)}
        metadata = {"png": png}
        checks = []

        def add(rule, match, expected, actual, reason="", code="nonconformance"):
            item = {"rule_id": rule, "category": "size", "result": "passed" if match else "failed",
                    "expected": expected, "actual": actual, "basis": "user_delivery_standard_100ppi"}
            if not match:
                item.update(reason_code=code, reason=reason)
            checks.append(item)

        readable = not png.get("metadata_error")
        add("DELIVERY-01", readable, ["png"], ["png"] if readable else [],
            "须提供可读取的最终PNG；缺文件或文件损坏不能证明交付齐全", "missing_evidence")
        add("SIZE-01", pixels is not None, "明确最终宽×高mm，1:1原尺寸输出",
            [str(args.width), str(args.height)] if pixels else None,
            "缺少最终毫米尺寸，无法核实1:1交付", "missing_target_dimensions")
        actual_px = [png.get("width_px"), png.get("height_px")]
        add("PNG-01", pixels is not None and actual_px == pixels, pixels, actual_px,
            "PNG实际像素与最终mm按100 PPI分别计算、四舍五入的结果不符，或缺少必要证据")
        density = png.get("density")
        # PNG uses integer pixels/metre. 3937 ppm is approximately 99.9998 PPI.
        expected_density = {"x_ppm": 3937, "y_ppm": 3937, "unit": 1}
        add("PNG-02", density is not None and all(density.get(k) == v for k, v in expected_density.items()),
            expected_density, density, "PNG导出密度须为100 PPI；整数像素/米量化为3937约99.9998 PPI属正常取整")
        add("WATERMARK-01", False, "最终PNG无水印", None,
            "静态助手未执行此项视觉核对；须查看实际画面后另出完整审核", "check_not_executed")
        if args.role != "final":
            add("DELIVERY-02", False, "最终交付文件", args.role,
                "预览、截图或用途未知不能证明正式交付符合", "final_artifact_not_verified")
        categories = {
            "logo": {"result": "failed", "reason_code": "check_not_executed", "reason": "助手未执行Logo视觉核对"},
            "slogan": {"result": "failed", "reason_code": "check_not_executed", "reason": "助手未执行品牌口号视觉核对"},
            "size": {"label": "成品交付", "result": merge_results([c["result"] for c in checks]),
                     "reason": "检查包含最终PNG、毫米尺寸、100 PPI和无水印；未完成项见checks"},
        }
        report = {"report_type": "delivery_preflight_not_full_audit", "skill_version": "2.1.0",
                  "created_at": datetime.now(timezone.utc).isoformat(), "scope": list(CATEGORIES),
                  "baseline": {"source": "user_delivery_standard", "required_formats": ["png"],
                               "unit": "mm", "scale": "1:1", "png_ppi": 100},
                  "reviewed_artifacts": artifacts, "metadata": metadata, "checks": checks, "categories": categories,
                  "result": merge_results([c["result"] for c in categories.values()]),
                  "quality_notes": list(args.quality_note), "quality_policy": {"blocking": False, "affects_result": False},
                  "limitations": ["静态预检未解码图像像素，不代替PNG实际打开及Logo、口号、水印的视觉实审",
                                  "清晰度不足只记录，不计入失败、不阻止导出",
                                  "预检报告为可选内部记录，不增加最终PNG以外的成品交付要求"]}
        if pixels:
            report["target"] = {"width_mm": str(args.width), "height_mm": str(args.height), "scale": "1:1",
                                "png_ppi": 100, "expected_png_pixels": pixels}
        payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.out:
            with args.out.open("x", encoding="utf-8") as handle:
                handle.write(payload)
            print(str(args.out.resolve()))
        else:
            print(payload, end="")
        return 0
    except (OSError, ValueError, ArithmeticError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
