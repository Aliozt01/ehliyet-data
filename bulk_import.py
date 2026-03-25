#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Geçmiş yılların sınavlarını toplu olarak çeker.
Yıl sayfalarından (2019-2025) günlük sınavları ve
çıkmış sorular sayfasından (2013-2018) MEB sınavlarını toplar.

Kullanım:
  export GH_TOKEN=ghp_...
  python3 bulk_import.py
"""
import requests, json, time, re, sys, os, hashlib, base64
from bs4 import BeautifulSoup
from datetime import datetime
from collections import Counter

# ─── auto_scraper.py'dan import ─────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from auto_scraper import (
    GITHUB_TOKEN, GITHUB_USER, DATA_REPO, IMGS_REPO,
    CDN_BASE, HEADERS, BASE_URL, BASE,
    MONTH_NAMES_TR, MONTH_URL,
    gh_headers, gh_get_file, gh_put_file,
    fetch, scrape_exam, process_images_to_cdn,
    guess_category
)

# ─── Yıl sayfası URL'leri ────────────────────────────────────────────
YEAR_PAGES = {
    2025: f'{BASE_URL}/index.php/2025-ehliyet-sinavi-sorulari-coz.html',
    2024: f'{BASE_URL}/index.php/2024-ehliyet-sinavi-sorulari-coz.html',
    2023: f'{BASE_URL}/index.php/2023-ehliyet-sinavi-sorulari-coz.html',
    2022: f'{BASE_URL}/index.php/2022-ehliyet-sinav-sorulari-coz.html',
    2021: f'{BASE_URL}/index.php/2021-ehliyet-sinav-sorulari-coz.html',
    2020: f'{BASE_URL}/index.php/2020-ehliyet-sinav-sorulari-coz.html',
    2019: f'{BASE_URL}/index.php/2019-ehliyet-sinav-sorulari-coz.html',
}

CIKMIS_PAGE = f'{BASE_URL}/index.php/cikmis-ehliyet-sinav-sorulari.html'

MONTH_TR_TO_NUM = {
    'ocak':1,'subat':2,'mart':3,'nisan':4,'mayis':5,
    'haziran':6,'temmuz':7,'agustos':8,'eylul':9,
    'ekim':10,'kasim':11,'aralik':12,
    'şubat':2,'ağustos':8,'eylül':9,'aralık':12
}

def discover_year_exams(year, url):
    """Yıl sayfasından günlük sınav linklerini bul."""
    exams = []
    r = fetch(url)
    if not r or r.status_code != 200:
        print(f"  ❌ {year} sayfası çekilemedi!", file=sys.stderr)
        return exams

    soup = BeautifulSoup(r.text, 'html.parser')
    links = soup.find_all('a', href=True)
    
    # Günlük sınav patterni: e-sinav-{ay}-sinavi-{gun}.html
    date_pattern = re.compile(r'e-sinav-(\w+)-sinavi-(\d+)\.html')
    
    for link in links:
        m = date_pattern.search(link['href'])
        if m:
            month_name = m.group(1)
            day = int(m.group(2))
            
            month_num = None
            for num, name in MONTH_URL.items():
                if name == month_name:
                    month_num = num
                    break
            
            if month_num:
                date_str = f"{year}-{month_num:02d}-{day:02d}"
                full_url = link['href'] if link['href'].startswith('http') else BASE_URL + '/' + link['href'].lstrip('/')
                month_name_tr = MONTH_NAMES_TR.get(month_num, '')
                exams.append({
                    'date': date_str,
                    'url': full_url,
                    'title': f"{day} {month_name_tr} {year} Ehliyet Sınav Soruları"
                })
    
    # Tekrarları kaldır
    seen = set()
    unique = []
    for e in exams:
        if e['date'] not in seen:
            seen.add(e['date'])
            unique.append(e)
    
    return sorted(unique, key=lambda x: x['date'])


def discover_cikmis_exams():
    """Çıkmış sorular sayfasından 2018 öncesi MEB sınav linklerini bul."""
    exams = []
    r = fetch(CIKMIS_PAGE)
    if not r or r.status_code != 200:
        print("  ❌ Çıkmış sorular sayfası çekilemedi!", file=sys.stderr)
        return exams
    
    soup = BeautifulSoup(r.text, 'html.parser')
    links = soup.find_all('a', href=True)
    
    # Tarihli sınav patterni: {gün}-{ay}-{yıl}-ehliyet
    date_pattern = re.compile(r'/(\d+)-(\w+)-(\d{4})-ehliyet')
    
    for link in links:
        m = date_pattern.search(link['href'])
        if m:
            day = int(m.group(1))
            month_str = m.group(2).lower()
            year = int(m.group(3))
            
            if year >= 2019:  # 2019+ yıl sayfalarından çekiliyor
                continue
            
            month_num = MONTH_TR_TO_NUM.get(month_str)
            if month_num:
                date_str = f"{year}-{month_num:02d}-{day:02d}"
                full_url = link['href'] if link['href'].startswith('http') else BASE_URL + '/' + link['href'].lstrip('/')
                month_name_tr = MONTH_NAMES_TR.get(month_num, '')
                exams.append({
                    'date': date_str,
                    'url': full_url,
                    'title': f"{day} {month_name_tr} {year} Ehliyet Sınav Soruları"
                })
    
    # Tekrarları kaldır
    seen = set()
    unique = []
    for e in exams:
        if e['date'] not in seen:
            seen.add(e['date'])
            unique.append(e)
    
    return sorted(unique, key=lambda x: x['date'])


def main():
    print("🚀 Toplu geçmiş sınav import başlatıldı", file=sys.stderr)
    
    if not GITHUB_TOKEN:
        print("❌ GH_TOKEN ortam değişkeni bulunamadı!", file=sys.stderr)
        sys.exit(1)
    
    # 1. Mevcut verileri indir
    print("📥 Mevcut exam_data.json indiriliyor...", file=sys.stderr)
    content, data_sha = gh_get_file(DATA_REPO, 'exam_data.json')
    
    if content:
        data = json.loads(content)
        print(f"  Mevcut: {len(data['exams'])} sınav", file=sys.stderr)
    else:
        data = {'exams': [], 'totalExams': 0, 'totalQuestions': 0}
    
    existing_dates = {e['examDate'] for e in data['exams']}
    
    # 2. Tüm yeni sınavları keşfet
    all_new = []
    
    # 2a. Yıl sayfaları (2019-2025)
    for year in sorted(YEAR_PAGES.keys()):
        print(f"🔍 {year} yılı taranıyor...", file=sys.stderr)
        year_exams = discover_year_exams(year, YEAR_PAGES[year])
        new_for_year = [e for e in year_exams if e['date'] not in existing_dates]
        print(f"  Toplam: {len(year_exams)}, Yeni: {len(new_for_year)}", file=sys.stderr)
        all_new.extend(new_for_year)
        time.sleep(1)
    
    # 2b. Çıkmış sorular (2013-2018)
    print("🔍 Çıkmış MEB sınavları taranıyor (2013-2018)...", file=sys.stderr)
    cikmis = discover_cikmis_exams()
    new_cikmis = [e for e in cikmis if e['date'] not in existing_dates]
    print(f"  Toplam: {len(cikmis)}, Yeni: {len(new_cikmis)}", file=sys.stderr)
    all_new.extend(new_cikmis)
    
    if not all_new:
        print("✅ Yeni sınav bulunamadı, tüm veriler güncel!", file=sys.stderr)
        return
    
    print(f"\n🆕 Toplam {len(all_new)} yeni sınav bulundu!", file=sys.stderr)
    
    # 3. Sınavları işle (tarih sırasına göre)
    all_new.sort(key=lambda x: x['date'])
    
    added = 0
    failed = 0
    batch_size = 50  # Her 50 sınavda bir kaydet
    
    for idx, info in enumerate(all_new):
        print(f"  [{idx+1}/{len(all_new)}] ⬇️  {info['title'][:50]}...", end=' ', file=sys.stderr)
        sys.stderr.flush()
        
        try:
            questions, _ = scrape_exam(info['url'])
            if not questions:
                print("❌ Soru çekilemedi", file=sys.stderr)
                failed += 1
                continue
            
            parts = info['date'].split('-')
            year = parts[0]
            month_num = int(parts[1])
            
            exam_id = f"exam_{info['date'].replace('-', '')}"
            
            new_exam = {
                'id': exam_id,
                'title': info['title'],
                'examDate': info['date'],
                'month': f"{year}-{month_num:02d}",
                'year': year,
                'sourceUrl': info['url'],
                'questions': questions
            }
            
            # Görselleri CDN'e yükle
            print(f"🖼️", end=' ', file=sys.stderr)
            process_images_to_cdn(new_exam)
            
            data['exams'].append(new_exam)
            existing_dates.add(info['date'])
            added += 1
            
            print(f"✅ {len(questions)} soru", file=sys.stderr)
            
            # Her batch_size sınavda bir kaydet (veri kaybını önle)
            if added % batch_size == 0:
                print(f"\n💾 Ara kayıt ({added} sınav)...", file=sys.stderr)
                data['exams'].sort(key=lambda e: e['examDate'], reverse=True)
                data['totalExams'] = len(data['exams'])
                data['totalQuestions'] = sum(len(e['questions']) for e in data['exams'])
                json_str = json.dumps(data, ensure_ascii=False, indent=2)
                
                # Taze sha al
                _, fresh_sha = gh_get_file(DATA_REPO, 'exam_data.json')
                gh_put_file(DATA_REPO, 'exam_data.json', json_str,
                           f"Bulk import: +{added} sınav (devam ediyor)", fresh_sha)
                print(f"  ✅ Kaydedildi ({data['totalExams']} sınav, {data['totalQuestions']} soru)", file=sys.stderr)
            
            time.sleep(0.5)  # Nazik ol, sunucuyu yorma
            
        except Exception as e:
            print(f"❌ Hata: {e}", file=sys.stderr)
            failed += 1
            continue
    
    if added == 0:
        print("Yeni sınav eklenemedi.", file=sys.stderr)
        return
    
    # 4. Son kayıt
    data['exams'].sort(key=lambda e: e['examDate'], reverse=True)
    data['totalExams'] = len(data['exams'])
    data['totalQuestions'] = sum(len(e['questions']) for e in data['exams'])
    
    print(f"\n📤 Son kayıt ({data['totalExams']} sınav, {data['totalQuestions']} soru)...", file=sys.stderr)
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    _, fresh_sha = gh_get_file(DATA_REPO, 'exam_data.json')
    success = gh_put_file(DATA_REPO, 'exam_data.json', json_str,
                          f"Bulk import: +{added} sınav ({datetime.now().strftime('%Y-%m-%d')})", fresh_sha)
    
    if not success:
        # Dosyayı yerel olarak kaydet
        with open('/tmp/exam_data_backup.json', 'w') as f:
            f.write(json_str)
        print("❌ Push başarısız! Yedek: /tmp/exam_data_backup.json", file=sys.stderr)
        return
    
    # 5. Version güncelle
    ver_content, ver_sha = gh_get_file(DATA_REPO, 'version.json')
    version_info = json.loads(ver_content) if ver_content else {'version': 0}
    version_info['version'] = version_info.get('version', 0) + 1
    version_info['lastUpdate'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')
    version_info['examCount'] = data['totalExams']
    version_info['questionCount'] = data['totalQuestions']
    
    ver_json = json.dumps(version_info, ensure_ascii=False, indent=2)
    gh_put_file(DATA_REPO, 'version.json', ver_json,
                f"Version bump: v{version_info['version']} (bulk import)", ver_sha)
    
    print(f"\n✅ Tamamlandı! +{added} sınav eklendi (başarısız: {failed}). Toplam: {data['totalExams']} sınav, {data['totalQuestions']} soru", file=sys.stderr)


if __name__ == '__main__':
    main()
