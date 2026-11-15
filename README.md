# 附近機車停車（桃園）

打開網頁就會定位，列出附近的路邊機車停車格、收費和時段，點一下就能用 Google Maps 導航過去。

- 地圖：Leaflet + OpenStreetMap 圖資，不需要金鑰
- 資料：[桃園市路邊停車路段收費資訊](https://opendata.tycg.gov.tw/datalist/ffd3180e-75b0-419d-b680-2286bade7958)（交通局），只取「機車車格」
- 免費／其他停車點：OpenStreetMap 上標記為 `amenity=motorcycle_parking` 的地點
- 我的停車點：在地圖上長按即可新增，只存在自己的手機

## 路段位置怎麼來的

官方資料只有路名和起訖路口（例如「中央東路(元化路-延平路)」），沒有座標。
`scripts/build_data.py` 會從 OpenStreetMap 找出主路和兩條交叉路的交點，再沿主路切出中間那段。
算好的結果存在 `data/geocache.json`，之後每週只處理新增或改名的路段。
找不到交點的路段會用單一路口或地名推估，詳情頁會標示「位置為推估」。

## 更新

GitHub Actions 每週一凌晨跑一次 `scripts/build_data.py`，有變動就自動 commit，GitHub Pages 隨之更新。
也可以在 Actions 頁面手動按 *Run workflow*。

本機重建：

```bash
python3 scripts/build_data.py
python3 -m http.server 8000
```
