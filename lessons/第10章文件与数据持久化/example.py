"""第 10 章：安全地读写文本、JSON、pickle 与 CSV。"""

import csv
import json
import os
import pickle
import tempfile
from pathlib import Path


USERS: list[dict[str, object]] = [
    {"id": 1, "name": "Alice", "age": 28, "active": True, "note": "普通字段"},
    {
        "id": 2,
        "name": "Doe, Jane",
        "age": 35,
        "active": False,
        "note": '喜欢 "Python"\n也喜欢文件处理',
    },
]
USER_FIELDS = ("id", "name", "age", "active", "note")


def print_section(title: str) -> None:
    """用清楚的标题分隔终端输出。"""
    print(f"\n=== {title} ===")


def demonstrate_text_files(root: Path) -> None:
    """演示 UTF-8、覆盖/追加/仅新建模式，以及逐行读取。"""
    print_section("1. 文本文件与资源管理")
    text_path: Path = root / "notes.txt"

    # write_text 适合一次写入不大的文本；返回值是写入的字符数。
    initial_text = "第一行：你好\n第二行：Python\n"
    written_count: int = text_path.write_text(initial_text, encoding="utf-8")

    # "a" 不会清空已有内容。with 确保正常或异常退出时都会关闭文件。
    with text_path.open("a", encoding="utf-8") as file:
        file.write("第三行：追加内容\n")

    # 文件对象本身是可迭代对象，适合逐行处理大文件。
    numbered_lines: list[str] = []
    with text_path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            # 不使用 strip()，以免意外删除属于数据的行首、行尾空格。
            content: str = line.removesuffix("\n")
            numbered_lines.append(f"{line_number}: {content}")

    print("首次写入字符数:", written_count)
    print("逐行读取:", numbered_lines)

    # "x" 表达“只能新建”：目标已存在时明确失败，不会误覆盖。
    try:
        with text_path.open("x", encoding="utf-8") as file:
            file.write("这行不会执行")
    except FileExistsError:
        print("x 模式保护已有文件: FileExistsError")

    # 只在当前层能解释或恢复时捕获具体异常。
    try:
        (root / "missing.txt").read_text(encoding="utf-8")
    except FileNotFoundError as error:
        print("缺少可选文件，使用默认值:", error.filename is not None)


def validate_users(data: object) -> list[dict[str, object]]:
    """把解析结果变成经过业务规则校验的用户列表。"""
    if not isinstance(data, list):
        raise ValueError("顶层 JSON 必须是数组")

    expected_fields: set[Literal["id", "name", "age", "active", "note"]] = set(
        USER_FIELDS
    )
    seen_ids: set[int] = set()
    validated_users: list[dict[str, object]] = []

    for position, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {position} 个用户必须是对象")

        actual_fields: set[Any] = set(item)
        if actual_fields != expected_fields:
            missing: list[Literal["id", "name", "age", "active", "note"]] = sorted(
                expected_fields - actual_fields
            )
            extra: list[Any] = sorted(actual_fields - expected_fields)
            details: str = f"missing={missing}, extra={extra}"
            raise ValueError(f"第 {position} 个用户字段不符: {details}")

        user_id: Any = item["id"]
        name: Any = item["name"]
        age: Any = item["age"]
        active: Any = item["active"]
        note: Any = item["note"]

        # bool 是 int 的子类；type(...) is int 才不会把 True 当成 ID 1。
        if type(user_id) is not int or user_id <= 0:
            raise ValueError(f"第 {position} 个用户 id 必须是正整数")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"第 {position} 个用户 name 必须是非空字符串")
        if type(age) is not int or not 0 <= age <= 150:
            raise ValueError(f"第 {position} 个用户 age 必须是 0 到 150 的整数")
        if type(active) is not bool:
            raise ValueError(f"第 {position} 个用户 active 必须是布尔值")
        if not isinstance(note, str):
            raise ValueError(f"第 {position} 个用户 note 必须是字符串")
        if user_id in seen_ids:
            raise ValueError(f"用户 id 重复: {user_id}")

        seen_ids.add(user_id)
        validated_users.append(
            {"id": user_id, "name": name, "age": age, "active": active, "note": note}
        )

    return validated_users


def atomic_write_json(path: Path, data: object) -> None:
    """在目标的同一目录写完临时文件，再原子替换目标文件。"""
    temporary_path: Path | None = None
    try:
        # delete=False 让文件关闭后仍存在，随后才能用 replace() 移动它。
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()  # 把 Python 用户态缓冲区交给操作系统。
            os.fsync(file.fileno())  # 请求操作系统把该文件内容刷新到存储设备。

        # 临时文件与目标在同一文件系统，replace 不会暴露半成品。
        temporary_path.replace(path)
    finally:
        # 成功替换后原临时路径已不存在；失败时则在这里清理半成品。
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def demonstrate_json(root: Path) -> None:
    """演示 JSON 原子写入、语法错误与业务校验错误。"""
    print_section("2. JSON：解析、校验与原子替换")
    json_path: Path = root / "users.json"

    # 模拟已经存在的旧配置；正式写入不会先用 "w" 清空它。
    json_path.write_text('{"version": "old"}\n', encoding="utf-8")
    atomic_write_json(json_path, USERS)

    with json_path.open("r", encoding="utf-8") as file:
        raw_data: Any = json.load(file)
    loaded_users: list[dict[str, object]] = validate_users(raw_data)
    json_text: str = json_path.read_text(encoding="utf-8")
    print("JSON 校验后往返相等:", loaded_users == USERS)
    print("UTF-8 中文仍可直接阅读:", "普通字段" in json_text)

    # set 不能写入 JSON。失败发生在临时文件中，不会破坏正式文件。
    try:
        atomic_write_json(json_path, {"unsupported": {1, 2}})
    except TypeError:
        leftover_files: list[Path] = list(root.glob(f".{json_path.name}.*.tmp"))
        target_unchanged: bool = json_path.read_text(encoding="utf-8") == json_text
        safe_failure: bool = target_unchanged and not leftover_files
        print("写入失败保留旧文件并清理临时文件:", safe_failure)

    # 语法错误由 JSON 解析器报告行列；它和业务规则错误是两类问题。
    try:
        json.loads('{"id": 1,}')
    except json.JSONDecodeError as error:
        print(f"坏 JSON 位置: 第 {error.lineno} 行, 第 {error.colno} 列")

    # 这是合法 JSON/Python 数据，但 True 不能充当业务上的整数 ID。
    invalid_users: list[dict[str, Any]] = [
        {"id": True, "name": "Bob", "age": 30, "active": True, "note": "反例"}
    ]
    try:
        validate_users(invalid_users)
    except ValueError as error:
        print("业务校验拒绝合法但无效的数据:", error)


def demonstrate_pickle(root: Path) -> None:
    """只加载本程序刚写出的可信 pickle，并展示其 Python 专用特征。"""
    print_section("3. pickle：仅限可信 Python 数据")
    pickle_path: Path = root / "cache.pickle"
    python_only_data: dict[str, Any] = {
        "users": USERS,
        "favorite_ids": {1, 2},
        "point": (3, 4),
    }

    with pickle_path.open("wb") as file:
        pickle.dump(python_only_data, file, protocol=pickle.HIGHEST_PROTOCOL)
    with pickle_path.open("rb") as file:
        # 安全边界：该文件由当前进程刚写出，不接受外部内容。
        loaded_data: Any = pickle.load(file)

    print("pickle 往返相等:", loaded_data == python_only_data)
    print(
        "pickle 保留 set/tuple:",
        type(loaded_data["favorite_ids"]).__name__,
        end=" / ",
    )
    print(type(loaded_data["point"]).__name__)

    # JSON 没有 tuple 类型，因此元组会退化为 list。
    json_point: Any = json.loads(json.dumps({"point": (3, 4)}))
    print("JSON 中 tuple 读回类型:", type(json_point["point"]).__name__)
    print("安全提醒：永远不要 pickle.load() 不可信文件")


def parse_csv_boolean(value: str, *, line_number: int) -> bool:
    """显式解析 CSV 布尔文本，避免 bool('False') 仍为 True。"""
    normalized: str = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"CSV 第 {line_number} 行布尔值无效: {value!r}")


def parse_csv_user(
    row: dict[str, str | None], *, line_number: int
) -> dict[str, object]:
    """把一行 CSV 字符串转换成业务类型，并给错误附加行号。"""
    if len(row) != len(USER_FIELDS):
        raise ValueError(f"CSV 第 {line_number} 行存在多余字段")

    missing_values: list[str] = [
        field for field in USER_FIELDS if row.get(field) is None
    ]
    if missing_values:
        raise ValueError(f"CSV 第 {line_number} 行缺少值: {missing_values}")

    # 上面的检查已排除 None；局部变量和 assert 让运行时前提更清楚。
    id_text: str | None = row["id"]
    name: str | None = row["name"]
    age_text: str | None = row["age"]
    active_text: str | None = row["active"]
    note: str | None = row["note"]
    assert id_text is not None and name is not None and age_text is not None
    assert active_text is not None and note is not None

    try:
        user_id = int(id_text)
    except ValueError as error:
        raise ValueError(f"CSV 第 {line_number} 行 id 不是整数: {id_text!r}") from error

    try:
        age = int(age_text)
    except ValueError as error:
        raise ValueError(f"CSV 第 {line_number} 行 age 不是整数: {age_text!r}") from error

    return {
        "id": user_id,
        "name": name,
        "age": age,
        "active": parse_csv_boolean(active_text, line_number=line_number),
        "note": note,
    }


def demonstrate_csv(root: Path) -> None:
    """演示 CSV 引号规则、原始字符串类型和显式类型转换。"""
    print_section("4. CSV：特殊字符与类型转换")
    csv_path: Path = root / "users.csv"

    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer: DictWriter[
            Literal["id", "name", "age", "active", "note"]
        ] = csv.DictWriter(file, fieldnames=USER_FIELDS)
        writer.writeheader()
        writer.writerows(USERS)

    physical_lines: list[str] = csv_path.read_text(encoding="utf-8").splitlines()
    line_with_comma: str = next(line for line in physical_lines if "Doe" in line)
    print("错误 split(',') 得到字段数:", len(line_with_comma.split(",")), "(实际应为 5)")
    print("字段内换行使物理行数:", len(physical_lines), "(逻辑记录含表头为 3)")

    converted_users: list[dict[str, object]] = []
    raw_types: dict[str, str] | None = None
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader: DictReader[str] = csv.DictReader(file)
        if reader.fieldnames != list(USER_FIELDS):
            raise ValueError(f"CSV 表头不符: {reader.fieldnames}")

        for row in reader:
            if raw_types is None:
                raw_types = {field: type(row[field]).__name__ for field in USER_FIELDS}
            converted_users.append(parse_csv_user(row, line_number=reader.line_num))

    # 格式层负责字符串转换，统一业务校验仍由 validate_users 负责。
    loaded_users: list[dict[str, object]] = validate_users(converted_users)
    print("CSV 原始字段类型:", raw_types)
    print("CSV 转换并校验后往返相等:", loaded_users == USERS)

    try:
        parse_csv_boolean("yes", line_number=99)
    except ValueError as error:
        print("坏 CSV 值包含定位信息:", error)


def demonstrate_readme_exercises(root: Path) -> None:
    """运行 README 中关于读取策略、年龄校验和异常路径的练习。"""
    print_section("5. README 小练习")

    print("练习 1：一次读取与逐行读取")
    text_path: Path = root / "exercise-notes.txt"
    text_path.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    complete_text: str = text_path.read_text(encoding="utf-8")
    with text_path.open("r", encoding="utf-8") as file:
        streamed_lines: list[str] = [line.removesuffix("\n") for line in file]
    print("  read_text 返回类型/长度:", type(complete_text).__name__, len(complete_text))
    print("  逐行读取返回:", streamed_lines)
    print("  两种方式内容一致:", complete_text.splitlines() == streamed_lines)

    print("练习 2：年龄字段的四种错误输入")
    valid_user: dict[str, Any] = {
        "id": 3,
        "name": "Cara",
        "age": 20,
        "active": True,
        "note": "年龄校验",
    }
    invalid_age_cases: dict[str, Any] = {
        "缺字段": {key: value for key, value in valid_user.items() if key != "age"},
        "布尔值": valid_user | {"age": True},
        "负数": valid_user | {"age": -1},
        "字符串": valid_user | {"age": "20"},
    }
    for label, invalid_user in invalid_age_cases.items():
        try:
            validate_users([invalid_user])
        except ValueError as error:
            print(f"  {label}:", error)

    print("练习 3：截断 JSON 的文件名、行号与列号")
    truncated_path: Path = root / "truncated.json"
    truncated_path.write_text('[\n  {"id": 1,\n', encoding="utf-8")
    try:
        with truncated_path.open("r", encoding="utf-8") as file:
            json.load(file)
    except json.JSONDecodeError as error:
        print(
            f"  {truncated_path.name}: 第 {error.lineno} 行, 第 {error.colno} 列 - {error.msg}"
        )

    print("练习 4：CSV 的特殊字段、物理行和坏布尔值结果见上方第 4 节")

    print("练习 5：连续原子替换时最后一次写入获胜")
    config_path: Path = root / "exercise-config.json"
    atomic_write_json(config_path, {"writer": "first"})
    atomic_write_json(config_path, {"writer": "second"})
    with config_path.open("r", encoding="utf-8") as file:
        final_config: Any = json.load(file)
    print("  最终配置:", final_config)
    print("  原子替换不等于并发锁；多写入者仍需额外协调")


def main() -> None:
    """在自动清理的目录中依次运行所有示例。"""
    with tempfile.TemporaryDirectory(prefix="python-files-") as directory:
        root = Path(directory)
        demonstrate_text_files(root)
        demonstrate_json(root)
        demonstrate_pickle(root)
        demonstrate_csv(root)
        demonstrate_readme_exercises(root)
        print_section("6. 临时目录")
        print("示例运行期间目录存在:", root.exists())

    print("退出 with 后目录已清理:", not root.exists())


if __name__ == "__main__":
    main()
