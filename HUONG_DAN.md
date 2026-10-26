# HƯỚNG DẪN CHẠY LẠI DỰ ÁN (Redis Cluster Caching)

Tài liệu ngắn để bạn tự chạy lại toàn bộ dự án và hiểu kết quả từng bước.
Mọi lệnh chạy **trong WSL** (Ubuntu), tại thư mục dự án.

## 0. Chuẩn bị

1. Mở Docker (Docker Desktop hoặc `sudo service docker start`).
2. Mở terminal WSL rồi vào thư mục dự án:

```bash
cd ~/redis-cluster-caching
docker info > /dev/null && echo "Docker OK"
```

Nếu thấy `Docker OK` là sẵn sàng.

## 1. Cách nhanh nhất: chạy tất cả bằng một lệnh

```bash
./scripts/run-all.sh --clean
```

- `--clean` = xóa container và dữ liệu cũ, chạy lại từ đầu (mất vài phút).
- Bỏ `--clean` nếu muốn giữ cluster đang chạy.
- Cuối cùng có bảng **PASS/FAIL** cho 7 giai đoạn. Thấy `OVERALL: PASS` là mọi thứ đều đúng.
- Nhật ký từng giai đoạn nằm trong `results/run-all/`.

## 2. Chạy từng bước (để giải thích khi thuyết trình)

| Bước | Lệnh | Mục đích |
|---|---|---|
| 1 | `./scripts/cluster-up.sh` | Bật 6 node Redis và tạo cluster |
| 2 | `./scripts/cluster-status.sh` | Xem trạng thái cluster |
| 3 | `docker compose exec app python demo_cache.py` | Demo cache |
| 4 | `./scripts/failover-demo.sh` | Demo node chính bị hỏng |
| 5 | `docker compose exec app python cache_problems.py` | 3 sự cố cache và cách sửa |
| 6 | `docker compose exec app python eviction_demo.py` | Xóa key khi đầy bộ nhớ (LRU/LFU) |
| 7 | `./scripts/benchmark.sh` | Đo tốc độ |
| – | `./scripts/cluster-down.sh` | Tắt cluster (giữ dữ liệu) |
| – | `./scripts/cluster-down.sh --volumes` | Tắt và xóa hết dữ liệu |

> Nếu bạn sửa file trong thư mục `app/`, phải build lại image:
> `docker compose build app && docker compose up -d --no-deps app`

## 3. Giải thích kết quả từng giai đoạn (nói đơn giản)

### Giai đoạn 1–2: Bật cluster và xem trạng thái
- Có **3 master** và **3 replica** (mỗi master có một bản sao dự phòng).
- `Slots assigned: 16384 / 16384` nghĩa là toàn bộ 16384 "ngăn" dữ liệu đã có chủ.
- `cluster_state:ok` nghĩa là cluster khỏe mạnh.

### Giai đoạn 3: Demo cache (`demo_cache.py`)
- Lần đọc thứ nhất: **MISS**, chậm (~200 ms) vì phải hỏi "database giả".
- Lần đọc thứ hai: **HIT**, rất nhanh (~1 ms) vì lấy từ Redis.
- Bảng cuối cho biết mỗi key nằm ở node nào → đây là **sharding** (chia dữ liệu ra nhiều node).

### Giai đoạn 4: Failover (`failover-demo.sh`)
- Script tắt một node master. Chương trình client đang ghi số liên tục sẽ báo `[ERROR]` trong khoảng 5-10 giây.
- Sau đó một replica tự động lên làm master mới và client ghi tiếp được (`[OK]`).
- Node bị tắt được bật lại và quay về làm replica.
- Mục "Data lost?" nên là **No** (không mất dữ liệu).
- Ý nghĩa: hệ thống **chịu lỗi**, một node chết vẫn chạy tiếp.

### Giai đoạn 5: Ba sự cố cache (`cache_problems.py`)
1. **Stampede (bão cache)**: 50 luồng cùng hỏi một key đã hết hạn.
   - Không bảo vệ: 50 lần gọi database. Dùng khóa (lock) `SET NX`: chỉ **1** lần gọi.
2. **Avalanche (tuyết lở cache)**: nhiều key hết hạn cùng lúc làm database bị quá tải.
   - TTL giống nhau: 200 key hết hạn trong cùng một giây. Thêm số ngẫu nhiên (jitter): hết hạn rải đều, mỗi giây chỉ khoảng 12 key.
3. **Penetration (xuyên cache)**: hỏi những id không tồn tại nên lần nào cũng tới database.
   - Sửa bằng cách lưu giá trị rỗng `__NULL__` trong 30 giây: từ 100 xuống còn **10** lần gọi database.

Mỗi phần kết thúc bằng `Result: PASS`.

### Giai đoạn 6: Eviction (`eviction_demo.py`)
- Script giới hạn bộ nhớ của một node rất nhỏ rồi nhét thật nhiều key vào, Redis buộc phải xóa bớt.
- **allkeys-lru** xóa key *lâu rồi không dùng* → key "hot" đã nghỉ vài giây bị xóa (`EVICTED`).
- **allkeys-lfu** xóa key *ít được dùng nhất* → key "hot" từng đọc 500 lần được giữ lại (`KEPT`).
- Cuối cùng script tự trả cấu hình về như cũ (`maxmemory=0`, `noeviction`).
- Lưu ý: trong lúc demo, một số key cache khác trên node đó có thể bị xóa. Chạy lại `demo_cache.py` để nạp lại.

### Giai đoạn 7: Benchmark (`benchmark.sh`)
- Đo số yêu cầu mỗi giây (requests/sec) của lệnh SET (ghi) và GET (đọc), 100000 yêu cầu, 50 client.
- Kết quả đầy đủ lưu ở `results/benchmark.txt`.
- Con số phụ thuộc vào máy của bạn (thường khoảng 130 nghìn – 200 nghìn yêu cầu/giây).
- SET và GET đôi khi ra đúng cùng một số vì công cụ đo thời gian theo mili giây và cả hai cùng chạy hết ~0,50 giây. Đây không phải lỗi.

## 4. Khi gặp lỗi

| Hiện tượng | Cách xử lý |
|---|---|
| `Cannot connect to the Docker daemon` | Docker chưa bật. Mở Docker Desktop hoặc `sudo service docker start` |
| `bad interpreter` / `\r: command not found` | File bị xuống dòng kiểu Windows. Chạy `sed -i 's/\r$//' scripts/*.sh` |
| Tên container đã tồn tại / xung đột | `./scripts/cluster-down.sh` rồi chạy lại |
| `cluster_state` không phải `ok` | Chạy `./scripts/cluster-status.sh`; nếu vẫn lỗi, làm lại từ đầu: `./scripts/run-all.sh --clean` |
| Dữ liệu cũ gây lỗi lạ | `./scripts/cluster-down.sh --volumes` rồi `./scripts/cluster-up.sh` |

Chi tiết hơn xem phần **Troubleshooting** trong `README.md`.
