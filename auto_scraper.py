#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Otomatik sınav scraper — GitHub Actions'ta çalışır.
1. Mevcut exam_data.json indirir (ehliyet-data repo'dan)
2. ehliyetsinavihazirlik.com'da yeni sınavları arar
3. Yeni sorular + görselleri çeker
4. Görselleri ehliyet-imgs repo'ya push eder
5. exam_data.json ve version.json günceller → ehliyet-data repo'ya push
"""
import requests, json, time, re, sys, os, hashlib, base64
from bs4 import BeautifulSoup
from datetime import datetime
from collections import Counter

# ─── Config ───────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ.get('GH_TOKEN', '')
GITHUB_USER = 'Aliozt01'
DATA_REPO = 'ehliyet-data'
IMGS_REPO = 'ehliyet-imgs'
CDN_BASE = f'https://cdn.jsdelivr.net/gh/{GITHUB_USER}/{IMGS_REPO}@main'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120',
    'Accept-Language': 'tr-TR,tr;q=0.9',
}
BASE_URL = 'https://ehliyetsinavihazirlik.com'
BASE = f'{BASE_URL}/index.php'

MONTH_NAMES_TR = {
    1: 'Ocak', 2: 'Şubat', 3: 'Mart', 4: 'Nisan', 5: 'Mayıs',
    6: 'Haziran', 7: 'Temmuz', 8: 'Ağustos', 9: 'Eylül',
    10: 'Ekim', 11: 'Kasım', 12: 'Aralık'
}
MONTH_URL = {
    1:'ocak',2:'subat',3:'mart',4:'nisan',5:'mayis',
    6:'haziran',7:'temmuz',8:'agustos',9:'eylul',
    10:'ekim',11:'kasim',12:'aralik'
}

# ─── GitHub API ───────────────────────────────────────────────────────
def gh_headers():
    return {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }

def gh_get_file(repo, path):
    """GitHub'dan dosya içeriğini ve sha'sını al.
    Büyük dosyalar (>1MB) için raw download URL kullanır.
    """
    url = f'https://api.github.com/repos/{GITHUB_USER}/{repo}/contents/{path}'
    r = requests.get(url, headers=gh_headers())
    if r.status_code != 200:
        print(f"  ⚠️ gh_get_file HTTP {r.status_code}: {path}", file=sys.stderr)
        return None, None
    
    data = r.json()
    sha = data.get('sha', '')
    
    # Küçük dosyalar: content alanı base64 olarak gelir
    if 'content' in data and data.get('encoding') == 'base64':
        try:
            content = base64.b64decode(data['content']).decode('utf-8')
            return content, sha
        except Exception as e:
            print(f"  ⚠️ base64 decode hatası: {e}", file=sys.stderr)
    
    # Büyük dosyalar (>1MB): download_url veya git blob kullan
    download_url = data.get('download_url')
    if download_url:
        print(f"  📦 Büyük dosya, raw URL ile indiriliyor...", file=sys.stderr)
        r2 = requests.get(download_url, headers={
            'Authorization': f'token {GITHUB_TOKEN}'
        }, timeout=60)
        if r2.status_code == 200:
            return r2.text, sha
        else:
            print(f"  ⚠️ Raw download hatası: HTTP {r2.status_code}", file=sys.stderr)
    
    # Fallback: git blob API kullan
    git_url = data.get('git_url')
    if git_url:
        print(f"  📦 Git blob API ile indiriliyor...", file=sys.stderr)
        r3 = requests.get(git_url, headers=gh_headers())
        if r3.status_code == 200:
            blob = r3.json()
            if blob.get('encoding') == 'base64':
                content = base64.b64decode(blob['content']).decode('utf-8')
                return content, sha
    
    print(f"  ❌ Dosya indirilemedi: {path}", file=sys.stderr)
    return None, None

def gh_put_file(repo, path, content, message, sha=None):
    """GitHub'a dosya yükle veya güncelle.
    Büyük dosyalar için Git Data API (blob + tree + commit) kullanır.
    """
    content_bytes = content.encode('utf-8')
    
    # 50MB'den küçük dosyalar için Contents API dene
    if len(content_bytes) < 50_000_000:
        url = f'https://api.github.com/repos/{GITHUB_USER}/{repo}/contents/{path}'
        payload = {
            'message': message,
            'content': base64.b64encode(content_bytes).decode('utf-8')
        }
        if sha:
            payload['sha'] = sha
        r = requests.put(url, headers=gh_headers(), json=payload, timeout=120)
        if r.status_code in (200, 201):
            return True
        print(f"  ⚠️ Contents API put hatası: HTTP {r.status_code}", file=sys.stderr)
    
    # Fallback: Git Data API (blob → tree → commit → update ref)
    print(f"  📦 Git Data API ile yükleniyor...", file=sys.stderr)
    try:
        # 1. Blob oluştur
        blob_url = f'https://api.github.com/repos/{GITHUB_USER}/{repo}/git/blobs'
        blob_r = requests.post(blob_url, headers=gh_headers(), json={
            'content': base64.b64encode(content_bytes).decode('utf-8'),
            'encoding': 'base64'
        }, timeout=120)
        if blob_r.status_code != 201:
            print(f"  ❌ Blob oluşturulamadı: {blob_r.status_code}", file=sys.stderr)
            return False
        blob_sha = blob_r.json()['sha']
        
        # 2. Mevcut HEAD commit'i al
        ref_url = f'https://api.github.com/repos/{GITHUB_USER}/{repo}/git/ref/heads/main'
        ref_r = requests.get(ref_url, headers=gh_headers())
        head_sha = ref_r.json()['object']['sha']
        
        commit_url = f'https://api.github.com/repos/{GITHUB_USER}/{repo}/git/commits/{head_sha}'
        commit_r = requests.get(commit_url, headers=gh_headers())
        tree_sha = commit_r.json()['tree']['sha']
        
        # 3. Yeni tree oluştur
        tree_url = f'https://api.github.com/repos/{GITHUB_USER}/{repo}/git/trees'
        tree_r = requests.post(tree_url, headers=gh_headers(), json={
            'base_tree': tree_sha,
            'tree': [{'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob_sha}]
        })
        new_tree_sha = tree_r.json()['sha']
        
        # 4. Yeni commit oluştur
        new_commit_url = f'https://api.github.com/repos/{GITHUB_USER}/{repo}/git/commits'
        new_commit_r = requests.post(new_commit_url, headers=gh_headers(), json={
            'message': message,
            'tree': new_tree_sha,
            'parents': [head_sha]
        })
        new_commit_sha = new_commit_r.json()['sha']
        
        # 5. ref güncelle
        update_r = requests.patch(ref_url, headers=gh_headers(), json={
            'sha': new_commit_sha
        })
        return update_r.status_code == 200
    except Exception as e:
        print(f"  ❌ Git Data API hatası: {e}", file=sys.stderr)
        return False

def gh_upload_image(img_url, filename):
    """Görseli indir ve ehliyet-imgs repo'ya yükle."""
    try:
        r = requests.get(img_url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        
        img_content = base64.b64encode(r.content).decode('utf-8')
        url = f'https://api.github.com/repos/{GITHUB_USER}/{IMGS_REPO}/contents/{filename}'
        
        # Dosya var mı kontrol et
        check = requests.get(url, headers=gh_headers())
        if check.status_code == 200:
            return f'{CDN_BASE}/{filename}'
        
        payload = {
            'message': f'Add {filename}',
            'content': img_content
        }
        r = requests.put(url, headers=gh_headers(), json=payload)
        if r.status_code in (200, 201):
            return f'{CDN_BASE}/{filename}'
    except Exception as e:
        print(f'  Görsel yükleme hatası: {e}', file=sys.stderr)
    return None

# ─── Scraping ─────────────────────────────────────────────────────────

def fetch(url, method='GET', data=None):
    for _ in range(3):
        try:
            kw = {'headers': HEADERS, 'timeout': 30}
            r = requests.post(url, data=data, **kw) if method == 'POST' else requests.get(url, **kw)
            r.encoding = 'utf-8'
            if r.status_code >= 400:
                print(f"  ⚠️ HTTP {r.status_code} ({url[:50]})", file=sys.stderr)
            return r
        except Exception as e:
            print(f"  ⚠️ fetch hatası ({url[:50]}): {e}", file=sys.stderr)
            time.sleep(2)
    return None

def parse_question_block(q_div, q_idx):
    q = {'num': q_idx + 1, 'text': '', 'choices': {}, 'correct': '',
         'category': '', 'imageUrl': '', 'videoUrl': '', 'explanation': ''}
    
    images = q_div.find_all('img')
    for img in images:
        src = img.get('src', '')
        if src and ('sorular' in src or 'animasyon' in src or '/images/' in src):
            if not src.startswith('http'):
                src = BASE_URL + src
            q['imageUrl'] = src
            break
    
    iframe = q_div.find('iframe')
    if iframe:
        vsrc = iframe.get('src', '')
        if 'vimeo' in vsrc or 'youtube' in vsrc:
            q['videoUrl'] = vsrc
    
    strong = q_div.find('strong')
    if strong:
        q['text'] = strong.get_text().strip()
    
    q_count = q_div.find('span', class_='simplequiz_question_count')
    if not q['text'] and q_count:
        texts = []
        for el in q_count.next_siblings:
            t = el.get_text().strip() if hasattr(el, 'get_text') else str(el).strip()
            if t and len(t) > 3:
                texts.append(t)
        q['text'] = ' '.join(texts[:2])
    
    labels = q_div.find_all('label', class_='sq_label') or q_div.find_all('label')
    choice_re = re.compile(r'^([A-D])\)\s*(.+)$', re.DOTALL)
    for label in labels:
        # Görselli şık kontrolü
        img = label.find('img')
        if img:
            src = img.get('src', '')
            if src and not src.startswith('http'):
                src = BASE_URL + src
            letter_match = re.match(r'^([A-D])\)', label.get_text().strip())
            if letter_match and src:
                q['choices'][letter_match.group(1)] = f'[IMG]{src}'
                continue
        
        label_text = label.get_text().strip()
        m = choice_re.match(label_text)
        if m:
            q['choices'][m.group(1)] = m.group(2).strip()
    
    q['category'] = guess_category(q['text'])
    return q

def get_correct_answers(url, radio_names, quiz_id):
    pd = {'quiz_id': quiz_id, 'simplequiz_post': 'true',
          'submit_button': 'Sınavı Bitir!', 'user_name': '', 'user_email': ''}
    for n in radio_names: pd[n] = 'A'
    
    r = fetch(url, 'POST', pd)
    if not r: return []
    
    soup2 = BeautifulSoup(r.text, 'html.parser')
    answers = []
    for exp in soup2.find_all('div', class_='sq_result_explanation'):
        # Açıklama metnini de al
        explanation_text = ''
        exp_div = exp.find('div', class_='sq_explanation')
        if exp_div:
            explanation_text = exp_div.get_text().strip()
        
        span = exp.find('span', class_='sq_correct_answer_text')
        if span:
            txt = span.get_text()
            if 'Tebrikler' in txt:
                answers.append(('A', explanation_text))
            else:
                idx = txt.find('DOĞRU CEVAP:')
                if idx != -1:
                    rest = txt[idx+12:].strip()
                    answers.append((rest[0] if rest and rest[0] in 'ABCD' else 'B', explanation_text))
                else:
                    answers.append(('B', explanation_text))
        else:
            answers.append(('A', explanation_text))
    return answers

def guess_category(text):
    t = text.lower()
    if any(w in t for w in ['ilk yardım','yaralı','kanama','solunum','kalp','bilinç','yanık','kırık','turnike']):
        return 'first_aid'
    if any(w in t for w in ['motor','akü','fren','lastik','radyatör','yağ','şanzıman','debriyaj','egzoz','abs','yakıt']):
        return 'engine_tech'
    if any(w in t for w in ['trafik adabı','sinirlen','öfke','sabır','stres','davranış','empati']):
        return 'traffic_manners'
    if any(w in t for w in ['levha','işaret','sinyal','kavşak','hız','park','sollama','şerit','alkol','ceza','ehliyet']):
        return 'traffic_rules'
    return 'traffic_env'

def scrape_exam(url):
    r = fetch(url)
    if not r or r.status_code != 200:
        return [], []
    
    soup = BeautifulSoup(r.text, 'html.parser')
    qi = soup.find('input', {'name': 'quiz_id'})
    if not qi: return [], []
    quiz_id = qi.get('value', '')
    
    radio_names = list(dict.fromkeys(
        i.get('name') for i in soup.find_all('input', type='radio') if i.get('name')
    ))
    
    questions = [parse_question_block(q_div, idx) 
                 for idx, q_div in enumerate(soup.find_all('div', class_='simplequiz_question'))]
    
    if radio_names:
        answers = get_correct_answers(url, radio_names, quiz_id)
        for i, q in enumerate(questions):
            if i < len(answers):
                q['correct'] = answers[i][0]
                if answers[i][1]:
                    q['explanation'] = answers[i][1]
    
    return questions, radio_names

def discover_new_exams(existing_dates):
    """Site ana sayfasından yeni sınav tarihlerini bul."""
    new_dates = []
    
    print(f"  Mevcut sınav tarihi sayısı: {len(existing_dates)}", file=sys.stderr)
    
    r = fetch(BASE_URL)
    if not r:
        print("  ❌ Ana sayfa çekilemedi!", file=sys.stderr)
        return new_dates
    
    print(f"  Ana sayfa yanıtı: HTTP {r.status_code}, {len(r.text)} byte", file=sys.stderr)
    
    soup = BeautifulSoup(r.text, 'html.parser')
    links = soup.find_all('a', href=True)
    
    print(f"  Toplam link sayısı: {len(links)}", file=sys.stderr)
    
    date_pattern = re.compile(r'e-sinav-(\w+)-sinavi-(\d+)\.html')
    matched_count = 0
    
    for link in links:
        m = date_pattern.search(link['href'])
        if m:
            matched_count += 1
            month_name = m.group(1)
            day = int(m.group(2))
            
            # ay adından ay numarası bul
            month_num = None
            for num, name in MONTH_URL.items():
                if name == month_name:
                    month_num = num
                    break
            
            if month_num:
                # Link metninden yılı bul
                link_text = link.get_text()
                year_match = re.search(r'20\d{2}', link_text)
                year = int(year_match.group()) if year_match else datetime.now().year
                
                date_str = f"{year}-{month_num:02d}-{day:02d}"
                if date_str not in existing_dates:
                    full_url = link['href'] if link['href'].startswith('http') else BASE_URL + '/' + link['href'].lstrip('/')
                    new_dates.append({
                        'date': date_str,
                        'url': full_url,
                        'title': link_text.strip()
                    })
    
    print(f"  Eşleşen link: {matched_count}, Yeni sınav: {len(new_dates)}", file=sys.stderr)
    
    # Aynı tarihi tekrar ekleme
    seen = set()
    unique = []
    for d in new_dates:
        if d['date'] not in seen:
            seen.add(d['date'])
            unique.append(d)
    
    return unique

def process_images_to_cdn(exam):
    """Sınavdaki görselleri CDN'e yükle ve URL'leri güncelle."""
    for q in exam.get('questions', []):
        # Soru görseli
        img_url = q.get('imageUrl', '')
        if img_url and 'ehliyetsinavihazirlik.com' in img_url:
            filename = hashlib.md5(img_url.encode()).hexdigest()[:12] + '_' + img_url.split('/')[-1]
            cdn_url = gh_upload_image(img_url, filename)
            if cdn_url:
                q['imageUrl'] = cdn_url
        
        # Şık görselleri
        for letter, choice_text in list(q.get('choices', {}).items()):
            if str(choice_text).startswith('[IMG]'):
                orig_url = choice_text[5:]
                if 'ehliyetsinavihazirlik.com' in orig_url:
                    filename = hashlib.md5(orig_url.encode()).hexdigest()[:12] + '_' + orig_url.split('/')[-1]
                    cdn_url = gh_upload_image(orig_url, filename)
                    if cdn_url:
                        q['choices'][letter] = f'[IMG]{cdn_url}'

# ─── MAIN ─────────────────────────────────────────────────────────────
def main():
    print("🚀 Otomatik sınav scraper başlatıldı", file=sys.stderr)
    
    if not GITHUB_TOKEN:
        print("❌ GH_TOKEN ortam değişkeni bulunamadı!", file=sys.stderr)
        sys.exit(1)
    
    # 1. Mevcut exam_data.json'u indir
    print("📥 Mevcut exam_data.json indiriliyor...", file=sys.stderr)
    content, data_sha = gh_get_file(DATA_REPO, 'exam_data.json')
    
    if content:
        data = json.loads(content)
        print(f"  Mevcut: {len(data['exams'])} sınav", file=sys.stderr)
    else:
        data = {'exams': [], 'totalExams': 0, 'totalQuestions': 0}
        print("  ⚠️ exam_data.json indirilemedi, boş başlatılıyor...", file=sys.stderr)
    
    # Version bilgisi
    ver_content, ver_sha = gh_get_file(DATA_REPO, 'version.json')
    version_info = json.loads(ver_content) if ver_content else {'version': 0, 'lastUpdate': '', 'examCount': 0, 'questionCount': 0}
    
    existing_dates = {e['examDate'] for e in data['exams']}
    
    # 2. Yeni sınavları keşfet
    print("🔍 Yeni sınavlar aranıyor...", file=sys.stderr)
    new_exam_infos = discover_new_exams(existing_dates)
    
    if not new_exam_infos:
        print("✅ Yeni sınav bulunamadı.", file=sys.stderr)
        return
    
    print(f"🆕 {len(new_exam_infos)} yeni sınav bulundu!", file=sys.stderr)
    
    added = 0
    for info in new_exam_infos:
        print(f"  ⬇️  {info['title'][:50]}...", end=' ', file=sys.stderr)
        sys.stderr.flush()
        
        questions, _ = scrape_exam(info['url'])
        if not questions:
            print("❌ Soru çekilemedi", file=sys.stderr)
            continue
        
        # ID oluştur
        exam_id = f"exam_{info['date'].replace('-', '')}"
        
        # Ay ve yıl
        parts = info['date'].split('-')
        year = parts[0]
        month_num = int(parts[1])
        month_name = MONTH_NAMES_TR.get(month_num, '')
        
        new_exam = {
            'id': exam_id,
            'title': info['title'] or f"{parts[2]} {month_name} {year} Ehliyet Sınav Soruları",
            'examDate': info['date'],
            'month': month_name,
            'year': year,
            'sourceUrl': info['url'],
            'questions': questions
        }
        
        # Görselleri CDN'e yükle
        print(f"🖼️  Görseller...", end=' ', file=sys.stderr)
        process_images_to_cdn(new_exam)
        
        data['exams'].append(new_exam)
        added += 1
        
        print(f"✅ {len(questions)} soru", file=sys.stderr)
        time.sleep(1)
    
    if added == 0:
        print("Yeni sınav eklenemedi.", file=sys.stderr)
        return
    
    # Tarihe göre sırala (yeniden eskiye)
    data['exams'].sort(key=lambda e: e['examDate'], reverse=True)
    data['totalExams'] = len(data['exams'])
    data['totalQuestions'] = sum(len(e['questions']) for e in data['exams'])
    
    # 3. exam_data.json güncelle
    print(f"📤 exam_data.json güncelleniyor ({data['totalExams']} sınav, {data['totalQuestions']} soru)...", file=sys.stderr)
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    success = gh_put_file(DATA_REPO, 'exam_data.json', json_str,
                          f"Auto-update: +{added} sınav ({datetime.now().strftime('%Y-%m-%d')})", data_sha)
    
    if not success:
        print("❌ exam_data.json push hatası!", file=sys.stderr)
        sys.exit(1)
    
    # 4. version.json güncelle
    version_info['version'] = version_info.get('version', 0) + 1
    version_info['lastUpdate'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')
    version_info['examCount'] = data['totalExams']
    version_info['questionCount'] = data['totalQuestions']
    
    ver_json = json.dumps(version_info, ensure_ascii=False, indent=2)
    gh_put_file(DATA_REPO, 'version.json', ver_json,
                f"Version bump: v{version_info['version']}", ver_sha)
    
    print(f"\n✅ Tamamlandı! {added} yeni sınav eklendi. Version: {version_info['version']}", file=sys.stderr)

if __name__ == '__main__':
    main()
