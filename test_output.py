# test_output.py - 修改为持续运行的脚本
import time
import sys

print("开始输出测试...")
sys.stdout.flush()

try:
    i = 0
    while True:  # 改为无限循环
        i += 1
        print(f"[{time.strftime('%H:%M:%S')}] 这是第 {i} 次输出11111111111")
        sys.stdout.flush()
        time.sleep(1)
except KeyboardInterrupt:
    print("\n收到中断信号，退出...")
    sys.exit(0)
