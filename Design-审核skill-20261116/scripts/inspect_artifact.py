#!/usr/bin/env python3
"""Read-only PNG + SVG preflight: 1:1 mm, PNG 150 PPI, binary results."""
import argparse
import base64
import binascii
import hashlib
import json
import math
import re
import struct
import sys
import zlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from urllib.parse import unquote_to_bytes

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


def local(tag):
    return tag.rsplit("}", 1)[-1]


def mm_value(value):
    match = re.fullmatch(r"\s*([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*mm\s*", value or "")
    if not match:
        return None
    number = Decimal(match.group(1))
    return number if number.is_finite() and number > 0 else None


def raster_dimensions(data):
    if data.startswith(PNG_SIGNATURE) and len(data) >= 33 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        if width and height:
            return width, height
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        if width and height:
            return width, height
    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 255:
                offset += 1
                continue
            while offset < len(data) and data[offset] == 255:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            if marker in (0xD9, 0xDA) or offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and length >= 7:
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                if width and height:
                    return width, height
            offset += length
    return None


def image_data(href):
    header, separator, payload = href.partition(",")
    if not separator or not header.lower().startswith("data:image/"):
        raise ValueError("图片未作为有效data:image资源嵌入")
    if ";base64" in header.lower():
        data = base64.b64decode("".join(payload.split()), validate=True)
    else:
        data = unquote_to_bytes(payload)
    if not data:
        raise ValueError("嵌入图片为空")
    return header[5:].split(";", 1)[0].lower(), data


def target_pixels(width, height):
    with localcontext() as context:
        context.prec = max(28, *(max(0, n.adjusted()) + len(n.as_tuple().digits) + 20 for n in (width, height)))
        pixels = [int((n / Decimal("25.4") * 150).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                  for n in (width, height)]
    if min(pixels) < 1:
        raise ValueError("换算后的成品宽高至少为1像素，不自动改大尺寸")
    return pixels


def svg_metadata(path):
    root = ET.fromstring(path.read_bytes())
    if local(root.tag) != "svg":
        raise ValueError("文件根元素不是svg")
    external, resource_errors, images, text_nodes = [], [], [], []
    foreign_objects = 0
    vector_count = 0
    css_needs_review = False

    def scan(tree, raw, depth=0):
        nonlocal foreign_objects, vector_count, css_needs_review
        if depth > 8:
            resource_errors.append("嵌套SVG超过静态检查深度，需另行核实无外部依赖")
            return
        if b"xml-stylesheet" in raw.replace(b"\x00", b""):
            resource_errors.append("含XML样式表处理指令，需核实是否外部渲染依赖")
        for node in tree.iter():
            tag = local(node.tag)
            if tag in ("text", "tspan", "textPath"):
                text_nodes.append({"element": tag, "id": node.get("id"), "nested_svg_depth": depth})
            if tag == "foreignObject":
                foreign_objects += 1
            if tag in ("path", "rect", "circle", "ellipse", "polygon", "polyline", "line", "use"):
                vector_count += 1
            references = []
            for name, value in node.attrib.items():
                attr = local(name)
                if attr in ("href", "src") and tag != "a" and value.strip():
                    references.append(value.strip())
            css = node.get("style", "") + ("\n" + "".join(node.itertext()) if tag == "style" else "")
            if "@import" in css.lower():
                resource_errors.append("含CSS @import，需移除外部依赖或完整嵌入样式")
            if "\\" in css:
                css_needs_review = True
            references.extend(re.findall(r"url\(\s*['\"]?([^\s'\"\)]+)", css, flags=re.I))
            for name, value in node.attrib.items():
                if local(name) != "style":
                    references.extend(re.findall(r"url\(\s*['\"]?([^\s'\"\)]+)", value, flags=re.I))
            for href in set(references):
                if href.startswith("#"):
                    continue
                if not href.lower().startswith("data:"):
                    external.append({"element": tag, "reference": href, "nested_svg_depth": depth})
                    continue
                if not href.lower().startswith("data:image/"):
                    # Inline non-image resources are self-contained; actual rendering still needs review.
                    continue
                entry = {"element": tag, "id": node.get("id"), "nested_svg_depth": depth}
                try:
                    mime, data = image_data(href)
                    entry.update(embedded=True, mime_type=mime, embedded_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                    dimensions = raster_dimensions(data)
                    if dimensions:
                        entry["intrinsic_pixels"] = list(dimensions)
                    if mime == "image/svg+xml":
                        nested = ET.fromstring(data)
                        if local(nested.tag) != "svg":
                            raise ValueError("嵌入资源声称为SVG但根元素不符")
                        scan(nested, data, depth + 1)
                except (ValueError, binascii.Error, ET.ParseError, ArithmeticError) as exc:
                    entry.update(embedded=False, resource_error=str(exc))
                    resource_errors.append("嵌入图片数据无法读取：" + str(exc))
                images.append(entry)
            if tag in ("image", "feImage") and not any(local(k) == "href" and v.strip() for k, v in node.attrib.items()):
                resource_errors.append("image/feImage缺少有效图片引用")

    raw = path.read_bytes()
    scan(root, raw)
    width_mm, height_mm = mm_value(root.get("width")), mm_value(root.get("height"))
    return {"format": "svg", "width_attribute": root.get("width"), "height_attribute": root.get("height"),
            "width_mm": str(width_mm) if width_mm is not None else None,
            "height_mm": str(height_mm) if height_mm is not None else None,
            "view_box_attribute": root.get("viewBox"), "live_text_elements": text_nodes,
            "foreign_object_count": foreign_objects, "external_rendering_references": external,
            "resource_errors": resource_errors, "css_requires_manual_review": css_needs_review,
            "embedded_images": images, "vector_element_count": vector_count,
            "validation_scope": "静态结构与资源预检；需实际渲染核对转曲、两格式内容一致与无水印；不对素材清晰度设失败阈值"}


def merge_results(results):
    return "passed" if results and all(result == "passed" for result in results) else "failed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--png", type=Path)
    parser.add_argument("--svg", type=Path)
    parser.add_argument("--artifact", type=Path, help="兼容单文件输入，缺少另一格式仍不通过")
    parser.add_argument("--role", choices=("final", "preview", "screenshot", "unknown"), default="unknown")
    parser.add_argument("--width", type=positive, help="最终宽度mm")
    parser.add_argument("--height", type=positive, help="最终高度mm")
    parser.add_argument("--unit", choices=("mm",), default="mm")
    parser.add_argument("--quality-note", action="append", default=[], help="素材清晰度说明；不参与通过判定，不阻止导出")
    parser.add_argument("--out", type=Path, help="写入新JSON报告，不覆盖已有文件")
    args = parser.parse_args(argv)
    if not any((args.png, args.svg, args.artifact)):
        parser.error("至少提供PNG或SVG文件")
    if (args.width is None) != (args.height is None):
        parser.error("最终宽高mm须同时提供，不能猜测缺少的一边")
    if args.artifact:
        name = "svg" if args.artifact.suffix.lower() == ".svg" else "png"
        if getattr(args, name):
            parser.error("同一格式的输入重复")
        setattr(args, name, args.artifact)
    inputs = {kind: path.resolve() for kind, path in (("png", args.png), ("svg", args.svg)) if path is not None}
    if args.out and args.out.resolve() in inputs.values():
        parser.error("报告不能覆盖审核原文件")
    try:
        pixels = target_pixels(args.width, args.height) if args.width is not None else None
        metadata, artifacts = {}, []
        for kind, path in inputs.items():
            try:
                digest = sha256_file(path)
                artifacts.append({"path": str(path), "format": kind, "sha256": digest, "role": args.role})
                parsed = png_metadata(path) if kind == "png" else svg_metadata(path)
                if parsed.get("format") != kind:
                    raise ValueError("文件内容与指定交付格式不符")
                metadata[kind] = parsed
            except (OSError, ValueError, ET.ParseError, ArithmeticError) as exc:
                metadata[kind] = {"metadata_error": str(exc)}
        checks = []

        def add(rule, match, expected, actual, reason="", code="nonconformance"):
            item = {"rule_id": rule, "category": "size", "result": "passed" if match else "failed",
                    "expected": expected, "actual": actual, "basis": "user_delivery_standard_150ppi"}
            if not match:
                item.update(reason_code=code, reason=reason)
            checks.append(item)

        readable = {kind for kind, item in metadata.items() if not item.get("metadata_error")}
        add("DELIVERY-01", readable == {"png", "svg"}, ["png", "svg"], sorted(readable),
            "须同时提供可读取的PNG和SVG；缺格式或文件损坏不能证明交付齐全", "missing_evidence")
        add("SIZE-01", pixels is not None, "明确最终宽×高mm，1:1输出", [str(args.width), str(args.height)] if pixels else None,
            "缺少最终毫米尺寸，无法核实1:1交付", "missing_target_dimensions")
        png, svg = metadata.get("png", {}), metadata.get("svg", {})
        actual_px = [png.get("width_px"), png.get("height_px")]
        add("PNG-01", pixels is not None and actual_px == pixels, pixels, actual_px,
            "PNG实际像素与最终mm按150 PPI分别计算、四舍五入的结果不符，或缺少必要证据")
        density = png.get("density")
        # PNG uses integer pixels/metre. 5906 ppm is approximately 150.0124 PPI.
        expected_density = {"x_ppm": 5906, "y_ppm": 5906, "unit": 1}
        add("PNG-02", density is not None and all(density.get(k) == v for k, v in expected_density.items()),
            expected_density, density, "PNG导出密度须为150 PPI；整数像素/米量化为5906约150.0124 PPI属正常取整")
        root_mm = [svg.get("width_mm"), svg.get("height_mm")]
        match_mm = pixels is not None and None not in root_mm and [Decimal(v) for v in root_mm] == [args.width, args.height]
        add("SVG-01", match_mm, [str(args.width), str(args.height)] if pixels else "最终宽高mm", root_mm,
            "SVG根宽高须显式使用mm并与最终尺寸一致；不能仅提供viewBox或擅自按比例缩小")
        live_text = svg.get("live_text_elements")
        text_ok = bool(svg) and not svg.get("metadata_error") and not live_text and not svg.get("foreign_object_count")
        add("SVG-02", text_ok, "没有未转曲文字；描边/路径需另行核实", live_text,
            "有未转曲文字、foreignObject内容待核实，或SVG不可读；无text不单独证明已转曲",
            "missing_evidence" if not live_text else "nonconformance")
        embedding_ok = (bool(svg) and not svg.get("metadata_error") and not svg.get("external_rendering_references") and
                        not svg.get("resource_errors") and not svg.get("css_requires_manual_review"))
        add("SVG-03", embedding_ok, "图片嵌入，无外部渲染依赖",
            {"external_references": svg.get("external_rendering_references"), "resource_errors": svg.get("resource_errors"),
             "css_requires_manual_review": svg.get("css_requires_manual_review"), "embedded_images": svg.get("embedded_images")},
            "存在外部图片/样式/资源，嵌入数据不可读，或复杂CSS依赖尚未核实")
        for rule, expected in (("SVG-04", "真实可编辑文字已转为轮廓路径；不能将整张PNG封装SVG冒充转曲"),
                               ("CONTENT-01", "PNG和SVG实际渲染内容一致"),
                               ("WATERMARK-01", "两种格式均无水印")):
            add(rule, False, expected, None, "静态助手未执行此项视觉核对；须读取渲染结果后另出完整审核", "check_not_executed")
        if args.role != "final":
            add("DELIVERY-02", False, "最终交付文件", args.role, "预览、截图或用途未知不能证明正式交付符合", "final_artifact_not_verified")
        categories = {
            "logo": {"result": "failed", "reason_code": "check_not_executed", "reason": "助手未执行Logo视觉核对"},
            "slogan": {"result": "failed", "reason_code": "check_not_executed", "reason": "助手未执行品牌口号视觉核对"},
            "size": {"label": "成品交付", "result": merge_results([c["result"] for c in checks]), "reason": "检查包含双格式、尺寸、转曲、嵌入、内容一致和无水印；未完成项见checks"},
        }
        report = {"report_type": "delivery_preflight_not_full_audit", "skill_version": "2.0.0",
                  "created_at": datetime.now(timezone.utc).isoformat(), "scope": list(CATEGORIES),
                  "baseline": {"source": "user_delivery_standard", "required_formats": ["png", "svg"], "unit": "mm", "scale": "1:1", "png_ppi": 150},
                  "reviewed_artifacts": artifacts, "metadata": metadata, "checks": checks, "categories": categories,
                  "result": merge_results([c["result"] for c in categories.values()]),
                  "quality_notes": list(args.quality_note), "quality_policy": {"blocking": False, "affects_result": False},
                  "limitations": ["静态预检不代替Logo、口号、转曲、内容一致与水印的视觉实审", "清晰度不足只记录，不计入失败、不阻止导出", "没有检测到text不能单独证明活动文字为轮廓路径"]}
        if pixels:
            report["target"] = {"width_mm": str(args.width), "height_mm": str(args.height), "scale": "1:1", "png_ppi": 150, "expected_png_pixels": pixels}
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

