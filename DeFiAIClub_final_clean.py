import sys
import os
import json
import random
import time
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                            QLineEdit, QPushButton, QLabel, QTextEdit, QComboBox,
                            QGroupBox, QMessageBox, QFrame, QTabWidget, QTableWidget,
                            QTableWidgetItem, QHeaderView, QScrollArea, QCheckBox, 
                            QFileDialog, QSpinBox, QProgressBar, QDialog, QPlainTextEdit)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QUrl
from PyQt5.QtGui import QFont, QPalette, QColor, QDesktopServices

import requests

# =============================
# Configuration
# =============================

CONFIG_FILE = "defi_ai_config.json"

# =============================
# Dialog-first prompt database
# =============================

EMBEDDED_PROMPTS = [
    "привет, давай как два эксперта подебатим про будущее AI и влияние на общество",
    "окей, начну: какая роль человека остаётся, если агенты закрывают 90 процентов задач?",
    "интересно, но как ты смотришь на риски централизации моделей и доступов?",
    "хорошо, а теперь разверни мысль: как культура и искусство меняются, когда ИИ становится соавтором?",
    "ладно, с философией ясно, а что с образованием - как перестроить курсы под агентоцентричный мир?",
    "добавь контрудар: в каких кейсах человека точно не заменить и почему?",
    "окей, давай про практику: как бы ты построил безопасную систему AI-агентов для банка?",
    "смени угол: этика и правила - где граница между удобством и контролем?",
    "хорошо, теперь про экосистемы - как open-source и проприетарные модели уживутся?",
    "заверши раунд: каким будет рынок труда через 5 лет и какие навыки критичны?"
]

def load_prompts_from_file(path):
    prompts = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if len(line) > 280:
                    continue
                prompts.append(line)
    except Exception:
        pass
    return prompts if prompts else EMBEDDED_PROMPTS[:]

DEFAULT_PROMPTS_PATHS = [
    os.path.join(os.getcwd(), "prompts_base.txt"),
    os.path.join(os.path.dirname(__file__), "prompts_base.txt"),
    "/mnt/data/prompts_base.txt"
]
PROMPT_DATABASE = None
for p in DEFAULT_PROMPTS_PATHS:
    PROMPT_DATABASE = load_prompts_from_file(p)
    if PROMPT_DATABASE and PROMPT_DATABASE != EMBEDDED_PROMPTS:
        break
if PROMPT_DATABASE is None:
    PROMPT_DATABASE = EMBEDDED_PROMPTS[:]

# =============================
# Models & API config
# =============================

DEFAULT_NOUS_MODEL = "Hermes-4-70B"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-3.5-turbo"

SYSTEM_PREAMBLE = (
    "You are participating in a structured, respectful, concise expert debate. "
    "Each turn, reply in 2-6 sentences. Build directly on the peer's previous point. "
    "Avoid repetition; introduce one new argument or evidence per turn."
)

FOLLOWUP_USER_TEMPLATE = (
    "Оппонент только что сказал:\n\"{last}\"\n"
    "Сформулируй следующий короткий ход дискуссии, добавь 1 новый аргумент и 1 уточняющий вопрос."
)

# =============================
# Data classes
# =============================

class Account:
    def __init__(self, nous_key, openrouter_key, proxy, prompt, enabled=True):
        self.nous_key = nous_key
        self.openrouter_key = openrouter_key
        self.proxy = proxy
        self.prompt = prompt
        self.enabled = enabled
        self.usage_count = 0
        self.success_count = 0
        self.error_count = 0
        self.last_used = None
        self.response_times = []
        self.last_response_time = None

class AccountManager:
    def __init__(self):
        self.accounts = []
        
    def add_account(self, nous_key, openrouter_key, proxy, prompt, enabled=True):
        account = Account(nous_key, openrouter_key, proxy, prompt, enabled)
        self.accounts.append(account)
        return account
        
    def get_active_accounts(self):
        return [acc for acc in self.accounts if acc.enabled]
        
    def get_account_stats(self):
        active = len(self.get_active_accounts())
        total = len(self.accounts)
        return f"Аккаунты: {active}/{total} активны"

# =============================
# Worker threads
# =============================

class ProxyCheckThread(QThread):
    finished_signal = pyqtSignal(str, bool, str)  # proxy, success, message

    def __init__(self, proxy):
        super().__init__()
        self.proxy = proxy

    def run(self):
        try:
            # Convert host:port:user:pass to http://user:pass@host:port format
            proxy_parts = self.proxy.split(':')
            if len(proxy_parts) == 4:
                host, port, user, password = proxy_parts
                formatted_proxy = f"http://{user}:{password}@{host}:{port}"
            else:
                formatted_proxy = f"http://{self.proxy}"
            
            test_url = "https://httpbin.org/ip"
            proxy_dict = {
                "http": formatted_proxy,
                "https": formatted_proxy
            }
            
            start_time = time.time()
            response = requests.get(test_url, proxies=proxy_dict, timeout=15)
            response_time = time.time() - start_time
            
            if response.status_code == 200:
                data = response.json()
                self.finished_signal.emit(
                    self.proxy, 
                    True, 
                    f"✓ Работает ({response_time:.2f} сек) - IP: {data.get('origin', 'Unknown')}"
                )
            else:
                self.finished_signal.emit(
                    self.proxy, 
                    False, 
                    f"✗ Ошибка HTTP: {response.status_code}"
                )
                
        except requests.Timeout:
            self.finished_signal.emit(self.proxy, False, "✗ Таймаут соединения")
        except requests.ConnectionError:
            self.finished_signal.emit(self.proxy, False, "✗ Ошибка подключения")
        except Exception as e:
            self.finished_signal.emit(self.proxy, False, f"✗ Ошибка: {str(e)}")

class ConversationThread(QThread):
    update_signal = pyqtSignal(str, str)
    progress_signal = pyqtSignal(str, int)
    finished_signal = pyqtSignal(str, bool)
    stats_signal = pyqtSignal(str, float)

    def __init__(self, account, turns, thread_id, delay_range=(1, 3), nous_model=DEFAULT_NOUS_MODEL, or_model=DEFAULT_OPENROUTER_MODEL):
        super().__init__()
        self.account = account
        self.turns = turns
        self.thread_id = thread_id
        self.delay_range = delay_range
        self.running = True
        self.nous_model = nous_model
        self.or_model = or_model
        self.progress = 0

    def run(self):
        self.facilitate_conversation()

    def stop(self):
        self.running = False

    def format_proxy(self, proxy):
        """Convert host:port:user:pass to proper format"""
        if not proxy:
            return None
            
        proxy_parts = proxy.split(':')
        if len(proxy_parts) == 4:
            host, port, user, password = proxy_parts
            return f"http://{user}:{password}@{host}:{port}"
        elif len(proxy_parts) == 2:
            host, port = proxy_parts
            return f"http://{host}:{port}"
        else:
            return f"http://{proxy}"

    def validate_proxy(self, proxy):
        """Проверка работоспособности прокси"""
        try:
            formatted_proxy = self.format_proxy(proxy)
            if not formatted_proxy:
                return False
                
            test_url = "https://httpbin.org/ip"
            proxy_dict = {"http": formatted_proxy, "https": formatted_proxy}
            response = requests.get(test_url, proxies=proxy_dict, timeout=10)
            return response.status_code == 200
        except:
            return False

    def _make_messages(self, history):
        return history[-8:]

    def query_api(self, messages, api_type, api_key, model, proxy=None):
        """Улучшенный запрос к API с retry logic"""
        for attempt in range(3):
            try:
                time.sleep(random.uniform(0.4, 1.2))
                start_time = time.time()
                
                formatted_proxy = self.format_proxy(proxy) if proxy else None
                
                if api_type == "nousresearch":
                    url = "https://inference-api.nousresearch.com/v1/chat/completions"
                    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                else:
                    url = "https://openrouter.ai/api/v1/chat/completions"
                    headers = {
                        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                        "HTTP-Referer": "https://deficlub.pro", "X-Title": "DeFi AI Club"
                    }
                
                payload = {"model": model, "messages": messages, "max_tokens": 500}
                proxy_dict = {"http": formatted_proxy, "https": formatted_proxy} if formatted_proxy else None
                
                response = requests.post(url, headers=headers, json=payload, 
                                       proxies=proxy_dict, timeout=30)
                response.raise_for_status()
                
                response_time = time.time() - start_time
                self.stats_signal.emit(self.thread_id, response_time)
                
                time.sleep(random.uniform(*self.delay_range))
                data = response.json()
                return data['choices'][0]['message']['content']
                
            except requests.Timeout:
                if attempt == 2:
                    return "Ошибка: Таймаут соединения"
                time.sleep(2 ** attempt)
            except requests.ConnectionError:
                if attempt == 2:
                    return "Ошибка: Проблема с соединение"
                time.sleep(2 ** attempt)
            except requests.HTTPError as e:
                if e.response.status_code == 401:
                    return "Ошибка: Неверный API ключ"
                elif e.response.status_code == 429:
                    return "Ошибка: Лимит запросов превышен"
                elif attempt == 2:
                    return f"Ошибка HTTP: {str(e)}"
                time.sleep(2 ** attempt)
            except Exception as e:
                if attempt == 2:
                    error_type = type(e).__name__
                    return f"Ошибка {error_type}: {str(e)}"
                time.sleep(2 ** attempt)
        
        return "Ошибка: Неизвестная ошибка после нескольких попыток"

    def facilitate_conversation(self):
        account_id = self.account.nous_key[:8] + "..." if self.account.nous_key else (self.account.openrouter_key[:8] + "..." if self.account.openrouter_key else "no-key")
        proxy_info = f" через {self.account.proxy[:20]}..." if self.account.proxy else ""
        
        self.update_signal.emit(self.thread_id, f"👤 Аккаунт: {account_id}{proxy_info}")
        self.update_signal.emit(self.thread_id, f"💬 Стартовый промпт: {self.account.prompt}\n")

        history = [
            {"role": "system", "content": SYSTEM_PREAMBLE},
            {"role": "user", "content": self.account.prompt}
        ]

        current_api = "nousresearch"
        success = True
        last_assistant = ""

        for turn in range(self.turns):
            if not self.running:
                break
                
            self.progress_signal.emit(self.thread_id, int((turn / self.turns) * 100))
            self.update_signal.emit(self.thread_id, f"\n🔄 Раунд {turn + 1}/{self.turns}")
            
            try:
                if current_api == "nousresearch" and self.account.nous_key:
                    msgs = self._make_messages(history)
                    response = self.query_api(msgs, "nousresearch", self.account.nous_key, self.nous_model, self.account.proxy)
                    if "Ошибка:" in response:
                        self.update_signal.emit(self.thread_id, f"❌ Ошибка NousResearch: {response}")
                        success = False
                        break
                    self.update_signal.emit(self.thread_id, f"🤖 NousResearch:\n{response}\n")
                    history.append({"role": "assistant", "content": response})
                    last_assistant = response

                    follow = FOLLOWUP_USER_TEMPLATE.format(last=last_assistant.strip())
                    history.append({"role": "user", "content": follow})
                    current_api = "openrouter"

                elif current_api == "openrouter" and self.account.openrouter_key:
                    msgs = self._make_messages(history)
                    response = self.query_api(msgs, "openrouter", self.account.openrouter_key, self.or_model, self.account.proxy)
                    if "Ошибка:" in response:
                        self.update_signal.emit(self.thread_id, f"❌ Ошибка OpenRouter: {response}")
                        success = False
                        break
                    self.update_signal.emit(self.thread_id, f"🤖 OpenRouter:\n{response}\n")
                    history.append({"role": "assistant", "content": response})
                    last_assistant = response

                    follow = FOLLOWUP_USER_TEMPLATE.format(last=last_assistant.strip())
                    history.append({"role": "user", "content": follow})
                    current_api = "nousresearch"

                else:
                    self.update_signal.emit(self.thread_id, f"❌ Нет API ключа для {current_api}")
                    success = False
                    break
                
            except Exception as e:
                self.update_signal.emit(self.thread_id, f"💥 Критическая ошибка: {str(e)}")
                success = False
                break
        
        if success:
            self.update_signal.emit(self.thread_id, f"\n✅ Успешно завершено!")
            self.account.success_count += 1
        else:
            self.update_signal.emit(self.thread_id, f"\n❌ Провал!")
            self.account.error_count += 1
            
        self.account.usage_count += 1
        self.account.last_used = time.strftime("%H:%M:%S")
        self.progress_signal.emit(self.thread_id, 100)
        self.finished_signal.emit(self.thread_id, success)

# =============================
# FAQ Dialog
# =============================

class FAQDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("❓ FAQ - DeFi AI Club")
        self.setGeometry(200, 200, 800, 600)
        self.setStyleSheet("""
            QDialog { background-color: #0A0B12; color: #E4E6EB; }
            QLabel { color: #B9BED1; padding: 5px; }
            QPushButton { 
                background-color: #7B68EE; color: white; border: none; 
                border-radius: 8px; padding: 10px 20px; 
            }
        """)
        
        layout = QVBoxLayout()
        
        # FAQ content
        faq_text = QPlainTextEdit()
        faq_text.setReadOnly(True)
        faq_text.setPlainText(self.get_faq_content())
        faq_text.setStyleSheet("""
            QPlainTextEdit { 
                background-color: #1A1B26; 
                color: #E4E6EB; 
                border: 1px solid #7B68EE; 
                border-radius: 8px; 
                padding: 15px;
                font-family: 'Segoe UI';
                font-size: 12px;
            }
        """)
        
        layout.addWidget(faq_text)
        
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
        
        self.setLayout(layout)
    
    def get_faq_content(self):
        return """
🤖 DeFi AI Club - Часто задаваемые вопросы

🔑 Где взять API ключи?
• NousResearch: https://www.nousresearch.com/
• OpenRouter: https://openrouter.ai/

🌐 Какие прокси использовать?
• Формат: host:port:user:password
• Пример: 192.168.1.1:8080:myuser:mypass
• Рекомендуем резидентные прокси для лучшей анонимности

⚙️ Как настроить аккаунты?
1. Добавьте API ключи в соответствующие колонки
2. Укажите прокси в формате host:port:user:pass
3. Выберите или введите промпт

🔄 Как работает диалог?
• Программа автоматически переключается между API
• Каждый раунд - ответ от одного API на сообщение другого
• Система сохраняет контекст диалога

🚀 Рекомендуемые настройки:
• Раундов: 4-8 для естественного диалога
• Задержка: 2-5 секунд между запросами
• Потоков: 2-5 одновременно

❌ Частые ошибки:
• Неверные API ключи - проверьте на сайтах провайдеров
• Блокировка прокси - используйте проверенные прокси
• Лимиты запросов - соблюдайте лимиты API

💾 Сохранение данных:
• Данные автоматически сохраняются при закрытии
• Можно экспортировать в TXT для резервной копии

📊 Мониторинг:
• Следите за статистикой выполнения
• Проверяйте время ответа API
• Мониторьте успешные/неудачные запросы

Поддержка: https://deficlub.pro
        """

# =============================
# Main UI Class
# =============================

class DeFiAIClubMassUI(QWidget):
    def __init__(self):
        super().__init__()
        self.account_manager = AccountManager()
        self.active_threads = {}
        self.proxy_check_threads = {}
        self.thread_counter = 0
        self.response_times = []
        self.initUI()
        self.load_config()

    def initUI(self):
        # ... (existing style code remains exactly the same) ...
        # [Весь существующий код стилей остается без изменений]

        main_layout = QVBoxLayout()
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(16, 16, 16, 16)
        
        # Header with additional buttons
        header_layout = QHBoxLayout()
        header_widget = QWidget()
        header_layout_inner = QHBoxLayout(header_widget)
        header_layout_inner.setContentsMargins(12, 10, 12, 10)
        
        logo_label = QLabel("🤖💜 DeFi AI Club — Advanced Dialog Manager")
        logo_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #AEB2FF;")
        header_layout_inner.addWidget(logo_label)
        
        header_layout_inner.addStretch()
        
        # New buttons in header
        self.faq_btn = QPushButton("❓ FAQ")
        self.faq_btn.setFixedSize(60, 34)
        self.faq_btn.setStyleSheet("""
            QPushButton {
                background-color: #7B68EE;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 5px;
            }
            QPushButton:hover {
                background-color: #9370DB;
            }
        """)
        self.faq_btn.clicked.connect(self.show_faq)
        header_layout_inner.addWidget(self.faq_btn)
        
        self.save_btn = QPushButton("💾 Сохранить")
        self.save_btn.setFixedSize(80, 34)
        self.save_btn.setStyleSheet("""
            QPushButton {
                background-color: #7B68EE;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 5px;
            }
            QPushButton:hover {
                background-color: #9370DB;
            }
        """)
        self.save_btn.clicked.connect(self.save_config)
        header_layout_inner.addWidget(self.save_btn)
        
        self.load_btn = QPushButton("📂 Загрузить")
        self.load_btn.setFixedSize(80, 34)
        self.load_btn.setStyleSheet("""
            QPushButton {
                background-color: #7B68EE;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 5px;
            }
            QPushButton:hover {
                background-color: #9370DB;
            }
        """)
        self.load_btn.clicked.connect(self.load_config_dialog)
        header_layout_inner.addWidget(self.load_btn)
        
        stats_widget = QWidget()
        stats_layout = QHBoxLayout(stats_widget)
        stats_layout.setContentsMargins(0,0,0,0)
        self.header_stats = QLabel("Аккаунты: 0 | Активных: 0")
        self.header_stats.setStyleSheet("color: #C7CAEE; font-weight: 600;")
        stats_layout.addWidget(self.header_stats)
        header_layout_inner.addWidget(stats_widget)
        
        website_btn = QPushButton("🌐")
        website_btn.setFixedSize(34,34)
        website_btn.setStyleSheet("""
            QPushButton {
                background-color: #7B68EE;
                color: white;
                border: none;
                border-radius: 17px;
            }
            QPushButton:hover {
                background-color: #9370DB;
            }
        """)
        website_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://deficlub.pro")))
        header_layout_inner.addWidget(website_btn)
        
        header_layout.addWidget(header_widget)
        main_layout.addLayout(header_layout)

        # Main content area
        content_layout = QHBoxLayout()
        
        # Left sidebar with quick actions - UPDATED
        sidebar = QWidget()
        sidebar.setFixedWidth(250)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12,12,12,12)
        
        quick_actions = QGroupBox("⚡ Быстрые действия")
        quick_actions_layout = QVBoxLayout()
        
        quick_btns = [
            ("➕ Добавить аккаунт", self.add_account_row),
            ("🎲 Случайный промпт", self.apply_random_prompts),
            ("🔍 Проверить прокси", self.check_proxies),
            ("📥 Импорт промптов", self.import_prompts_from_txt),
            ("🗑️ Очистить все", self.clear_accounts),
            ("💾 Экспорт логов", self.export_results)
        ]
        
        for text, slot in quick_btns:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #7B68EE;
                    color: white;
                    border: none;
                    border-radius: 8px;
                    padding: 10px;
                    text-align: left;
                    margin: 2px;
                }
                QPushButton:hover {
                    background-color: #9370DB;
                }
            """)
            quick_actions_layout.addWidget(btn)
        
        quick_actions.setLayout(quick_actions_layout)
        sidebar_layout.addWidget(quick_actions)
        
        # Progress section
        progress_group = QGroupBox("📊 Прогресс")
        progress_layout = QVBoxLayout()
        
        self.global_progress = QProgressBar()
        progress_layout.addWidget(self.global_progress)
        
        self.active_threads_label = QLabel("Активных потоков: 0")
        progress_layout.addWidget(self.active_threads_label)
        
        progress_group.setLayout(progress_layout)
        sidebar_layout.addWidget(progress_group)
        
        sidebar_layout.addStretch()
        content_layout.addWidget(sidebar)

        # Main tabs area - ADDED PROXY CHECK TAB
        self.tab_widget = QTabWidget()
        
        # Accounts Tab
        accounts_tab = QWidget()
        accounts_layout = QVBoxLayout(accounts_tab)
        
        accounts_group = QGroupBox("🔐 Управление аккаунтами")
        accounts_group_layout = QVBoxLayout()
        
        # Create accounts table with 5 columns instead of 6
        self.accounts_table = QTableWidget()
        self.accounts_table.setColumnCount(5)
        self.accounts_table.setHorizontalHeaderLabels(["Вкл", "Nous Key", "OpenRouter Key", "Прокси", "Промпт"])
        self.accounts_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        accounts_group_layout.addWidget(self.accounts_table)
        
        accounts_group.setLayout(accounts_group_layout)
        accounts_layout.addWidget(accounts_group)
        
        # Prompt management
        prompt_group = QGroupBox("📝 Управление промптами")
        prompt_layout = QHBoxLayout()
        
        self.prompt_combo = QComboBox()
        self.prompt_combo.addItems(PROMPT_DATABASE)
        prompt_layout.addWidget(self.prompt_combo)
        
        apply_prompt_btn = QPushButton("Применить")
        apply_prompt_btn.setStyleSheet("""
            QPushButton {
                background-color: #7B68EE;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 5px;
            }
            QPushButton:hover {
                background-color: #9370DB;
            }
        """)
        apply_prompt_btn.clicked.connect(self.apply_selected_prompt)
        prompt_layout.addWidget(apply_prompt_btn)
        
        import_prompts_btn = QPushButton("Импорт из TXT")
        import_prompts_btn.setStyleSheet("""
            QPushButton {
                background-color: #7B68EE;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 5px;
            }
            QPushButton:hover {
                background-color: #9370DB;
            }
        """)
        import_prompts_btn.clicked.connect(self.import_prompts_from_txt)
        prompt_layout.addWidget(import_prompts_btn)
        
        prompt_group.setLayout(prompt_layout)
        accounts_layout.addWidget(prompt_group)
        
        accounts_tab.setLayout(accounts_layout)

        # Control Tab
        control_tab = QWidget()
        control_layout = QVBoxLayout(control_tab)
        
        settings_group = QGroupBox("⚙️ Настройки выполнения")
        settings_layout = QVBoxLayout()
        
        # Turns setting
        turns_layout = QHBoxLayout()
        turns_layout.addWidget(QLabel("Количество раундов:"))
        self.turns_input = QSpinBox()
        self.turns_input.setRange(1, 20)
        self.turns_input.setValue(4)
        turns_layout.addWidget(self.turns_input)
        turns_layout.addStretch()
        settings_layout.addLayout(turns_layout)
        
        # Delay setting
        delay_layout = QHBoxLayout()
        delay_layout.addWidget(QLabel("Задержка (сек):"))
        self.delay_input = QLineEdit("2-5")
        self.delay_input.setMaximumWidth(100)
        delay_layout.addWidget(self.delay_input)
        delay_layout.addStretch()
        settings_layout.addLayout(delay_layout)
        
        # Threads setting
        threads_layout = QHBoxLayout()
        threads_layout.addWidget(QLabel("Макс. потоков:"))
        self.threads_input = QSpinBox()
        self.threads_input.setRange(1, 20)
        self.threads_input.setValue(3)
        threads_layout.addWidget(self.threads_input)
        threads_layout.addStretch()
        settings_layout.addLayout(threads_layout)
        
        # Model settings
        model_layout = QHBoxLayout()
        model_layout.addWidget(QLabel("Nous Модель:"))
        self.nous_model_input = QLineEdit(DEFAULT_NOUS_MODEL)
        model_layout.addWidget(self.nous_model_input)
        model_layout.addStretch()
        settings_layout.addLayout(model_layout)
        
        # OpenRouter model (hidden but used internally)
        self.or_model_input = QLineEdit(DEFAULT_OPENROUTER_MODEL)
        self.or_model_input.setVisible(False)
        settings_layout.addWidget(self.or_model_input)
        
        # Additional options
        self.rotate_prompts = QCheckBox("Автоматически менять промпты при запуске")
        self.rotate_prompts.setChecked(True)
        settings_layout.addWidget(self.rotate_prompts)
        
        settings_group.setLayout(settings_layout)
        control_layout.addWidget(settings_group)
        
        # Control buttons
        control_buttons = QHBoxLayout()
        
        self.start_btn = QPushButton("🚀 Запуск")
        self.start_btn.setStyleSheet("""
            QPushButton {
                background-color: #32CD32;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #228B22;
            }
        """)
        self.start_btn.clicked.connect(self.start_all_accounts)
        control_buttons.addWidget(self.start_btn)
        
        self.stop_btn = QPushButton("⏹️ Стоп")
        self.stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #DC143C;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #B22222;
            }
        """)
        self.stop_btn.clicked.connect(self.stop_all_threads)
        control_buttons.addWidget(self.stop_btn)
        
        control_layout.addLayout(control_buttons)
        control_tab.setLayout(control_layout)

        # NEW: Proxy Check Tab
        proxy_tab = QWidget()
        proxy_layout = QVBoxLayout(proxy_tab)
        
        proxy_group = QGroupBox("🔍 Проверка прокси")
        proxy_group_layout = QVBoxLayout()
        
        proxy_help = QLabel("Введите прокси для проверки (по одному на строку, формат: host:port:user:pass):")
        proxy_group_layout.addWidget(proxy_help)
        
        self.proxy_input = QPlainTextEdit()
        self.proxy_input.setPlaceholderText("192.168.1.1:8080:user:pass\nproxy.example.com:3128:username:password")
        self.proxy_input.setMaximumHeight(100)
        proxy_group_layout.addWidget(self.proxy_input)
        
        check_btn = QPushButton("✅ Проверить прокси")
        check_btn.setStyleSheet("""
            QPushButton {
                background-color: #7B68EE;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px;
            }
            QPushButton:hover {
                background-color: #9370DB;
            }
        """)
        check_btn.clicked.connect(self.check_proxies_from_input)
        proxy_group_layout.addWidget(check_btn)
        
        self.proxy_results = QTextEdit()
        self.proxy_results.setReadOnly(True)
        self.proxy_results.setMaximumHeight(200)
        proxy_group_layout.addWidget(self.proxy_results)
        
        proxy_group.setLayout(proxy_group_layout)
        proxy_layout.addWidget(proxy_group)
        
        proxy_tab.setLayout(proxy_layout)
        
        # Output Tab
        output_tab = QWidget()
        output_layout = QVBoxLayout(output_tab)
        
        output_group = QGroupBox("📊 Лог выполнения")
        output_group_layout = QVBoxLayout()
        
        self.output_area = QTextEdit()
        self.output_area.setReadOnly(True)
        output_group_layout.addWidget(self.output_area)
        
        output_group.setLayout(output_group_layout)
        output_layout.addWidget(output_group)
        
        output_tab.setLayout(output_layout)
        
        # Add all tabs
        self.tab_widget.addTab(accounts_tab, "🔐 Аккаунты")
        self.tab_widget.addTab(control_tab, "⚙️ Управление")
        self.tab_widget.addTab(proxy_tab, "🔍 Прокси")
        self.tab_widget.addTab(output_tab, "📊 Лог")
        
        content_layout.addWidget(self.tab_widget)
        main_layout.addLayout(content_layout)
        
        self.setLayout(main_layout)
        self.setWindowTitle("DeFi AI Club — Advanced Dialog Manager")
        self.setGeometry(100, 100, 1600, 900)
        
        # Initialize
        self.add_account_row()
        self.update_stats()

    # =============================
    # NEW: Proxy Check Methods
    # =============================

    def check_proxies(self):
        """Проверить все прокси из таблицы"""
        proxies = set()
        for row in range(self.accounts_table.rowCount()):
            proxy = self.accounts_table.item(row, 3).text().strip() if self.accounts_table.item(row, 3) else ""
            if proxy and proxy not in proxies:
                proxies.add(proxy)
        
        if not proxies:
            QMessageBox.information(self, "Информация", "Нет прокси для проверки")
            return
        
        self.proxy_results.clear()
        self.proxy_results.append("🔍 Начинаю проверку прокси...\n")
        
        for proxy in proxies:
            thread = ProxyCheckThread(proxy)
            thread.finished_signal.connect(self.on_proxy_check_result)
            thread.start()
            self.proxy_check_threads[proxy] = thread

    def check_proxies_from_input(self):
        """Проверить прокси из текстового поля"""
        proxies_text = self.proxy_input.toPlainText().strip()
        if not proxies_text:
            QMessageBox.warning(self, "Ошибка", "Введите прокси для проверки")
            return
        
        proxies = [p.strip() for p in proxies_text.split('\n') if p.strip()]
        self.proxy_results.clear()
        self.proxy_results.append("🔍 Начинаю проверку прокси...\n")
        
        for proxy in proxies:
            thread = ProxyCheckThread(proxy)
            thread.finished_signal.connect(self.on_proxy_check_result)
            thread.start()
            self.proxy_check_threads[proxy] = thread

    def on_proxy_check_result(self, proxy, success, message):
        """Обработка результата проверки прокси"""
        color = "green" if success else "red"
        self.proxy_results.append(f"<font color='{color}'>{message}</font>")
        
        if proxy in self.proxy_check_threads:
            del self.proxy_check_threads[proxy]

    # =============================
    # NEW: Random Prompt Method
    # =============================

    def apply_random_prompts(self):
        """Применить случайные промпты ко всем аккаунтам"""
        if not PROMPT_DATABASE:
            QMessageBox.warning(self, "Ошибка", "Нет доступных промптов")
            return
        
        for row in range(self.accounts_table.rowCount()):
            random_prompt = random.choice(PROMPT_DATABASE)
            prompt_item = QTableWidgetItem(random_prompt)
            self.accounts_table.setItem(row, 4, prompt_item)
        
        self.output_area.append("🎲 Применены случайные промпты ко всем аккаунтам")

    # =============================
    # MODIFIED: Account Management Methods
    # =============================

    def add_account_row(self, nous_key="", openrouter_key="", proxy="", prompt="", enabled=True):
        row = self.accounts_table.rowCount()
        self.accounts_table.insertRow(row)
        
        # Enabled checkbox
        enabled_item = QTableWidgetItem()
        enabled_item.setCheckState(Qt.Checked if enabled else Qt.Unchecked)
        self.accounts_table.setItem(row, 0, enabled_item)
        
        # API keys
        self.accounts_table.setItem(row, 1, QTableWidgetItem(nous_key))
        self.accounts_table.setItem(row, 2, QTableWidgetItem(openrouter_key))
        
        # Proxy (new format)
        self.accounts_table.setItem(row, 3, QTableWidgetItem(proxy))
        
        # Prompt
        prompt_item = QTableWidgetItem(prompt if prompt else random.choice(PROMPT_DATABASE))
        self.accounts_table.setItem(row, 4, prompt_item)
        
        self.update_stats()

    def load_accounts_from_table(self):
        self.account_manager.accounts.clear()
        for row in range(self.accounts_table.rowCount()):
            enabled = self.accounts_table.item(row, 0).checkState() == Qt.Checked
            nous_key = self.accounts_table.item(row, 1).text().strip() if self.accounts_table.item(row, 1) else ""
            openrouter_key = self.accounts_table.item(row, 2).text().strip() if self.accounts_table.item(row, 2) else ""
            proxy = self.accounts_table.item(row, 3).text().strip() if self.accounts_table.item(row, 3) else ""
            prompt = self.accounts_table.item(row, 4).text().strip() if self.accounts_table.item(row, 4) else ""
            
            if nous_key or openrouter_key:
                self.account_manager.add_account(nous_key, openrouter_key, proxy, prompt, enabled)

    # =============================
    # MODIFIED: Configuration Methods
    # =============================

    def save_config(self):
        config = {
            "accounts": [],
            "turns": self.turns_input.value(),
            "delay": self.delay_input.text(),
            "max_threads": self.threads_input.value(),
            "nous_model": self.nous_model_input.text(),
            "or_model": self.or_model_input.text(),
            "rotate_prompts": self.rotate_prompts.isChecked()
        }
        
        for row in range(self.accounts_table.rowCount()):
            account = {
                "enabled": self.accounts_table.item(row, 0).checkState() == Qt.Checked,
                "nous_key": self.accounts_table.item(row, 1).text().strip() if self.accounts_table.item(row, 1) else "",
                "openrouter_key": self.accounts_table.item(row, 2).text().strip() if self.accounts_table.item(row, 2) else "",
                "proxy": self.accounts_table.item(row, 3).text().strip() if self.accounts_table.item(row, 3) else "",
                "prompt": self.accounts_table.item(row, 4).text().strip() if self.accounts_table.item(row, 4) else ""
            }
            config["accounts"].append(account)
        
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            self.output_area.append("💾 Конфигурация сохранена")
        except Exception as e:
            self.output_area.append(f"❌ Ошибка сохранения: {str(e)}")

    def load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = json.load(f)
                
                # Clear existing accounts
                self.accounts_table.setRowCount(0)
                
                # Load accounts
                for acc in config.get("accounts", []):
                    self.add_account_row(
                        acc.get("nous_key", ""),
                        acc.get("openrouter_key", ""),
                        acc.get("proxy", ""),
                        acc.get("prompt", ""),
                        acc.get("enabled", True)
                    )
                
                # Load settings
                self.turns_input.setValue(config.get("turns", 4))
                self.delay_input.setText(config.get("delay", "2-5"))
                self.threads_input.setValue(config.get("max_threads", 3))
                self.nous_model_input.setText(config.get("nous_model", DEFAULT_NOUS_MODEL))
                self.or_model_input.setText(config.get("or_model", DEFAULT_OPENROUTER_MODEL))
                self.rotate_prompts.setChecked(config.get("rotate_prompts", True))
                
                self.output_area.append("📂 Конфигурация загружена")
        except Exception as e:
            self.output_area.append(f"❌ Ошибка загрузки: {str(e)}")

    # =============================
    # NEW: Additional Methods
    # =============================

    def show_faq(self):
        dialog = FAQDialog(self)
        dialog.exec_()

    def load_config_dialog(self):
        options = QFileDialog.Options()
        file_name, _ = QFileDialog.getOpenFileName(
            self, "Загрузить конфигурацию", "", "JSON Files (*.json)", options=options
        )
        if file_name:
            global CONFIG_FILE
            CONFIG_FILE = file_name
            self.load_config()

    def apply_selected_prompt(self):
        """Применить выбранный промпт ко всем аккаунтам"""
        prompt = self.prompt_combo.currentText()
        for row in range(self.accounts_table.rowCount()):
            prompt_item = QTableWidgetItem(prompt)
            self.accounts_table.setItem(row, 4, prompt_item)
        
        self.output_area.append(f"📝 Применен промпт ко всем аккаунтам: {prompt[:50]}...")

    # =============================
    # EXISTING: Thread Management Methods (unchanged)
    # =============================

    def start_all_accounts(self):
        """Запуск всех аккаунтов"""
        self.load_accounts_from_table()
        active_accounts = self.account_manager.get_active_accounts()
        
        if not active_accounts:
            QMessageBox.warning(self, "Ошибка", "Нет активных аккаунтов для запуска")
            return
        
        max_threads = self.threads_input.value()
        delay_text = self.delay_input.text()
        
        try:
            if "-" in delay_text:
                min_delay, max_delay = map(float, delay_text.split("-"))
                delay_range = (min_delay, max_delay)
            else:
                delay = float(delay_text)
                delay_range = (delay, delay)
        except:
            delay_range = (2, 5)
        
        # Apply random prompts if enabled
        if self.rotate_prompts.isChecked() and PROMPT_DATABASE:
            for account in active_accounts:
                account.prompt = random.choice(PROMPT_DATABASE)
        
        # Update table with new prompts
        for row in range(self.accounts_table.rowCount()):
            if self.accounts_table.item(row, 0).checkState() == Qt.Checked:
                account = active_accounts.pop(0) if active_accounts else None
                if account:
                    prompt_item = QTableWidgetItem(account.prompt)
                    self.accounts_table.setItem(row, 4, prompt_item)
        
        self.load_accounts_from_table()
        active_accounts = self.account_manager.get_active_accounts()
        
        self.output_area.clear()
        self.output_area.append(f"🚀 Запуск {len(active_accounts)} аккаунтов...\n")
        
        # Start threads with limited concurrency
        for i, account in enumerate(active_accounts):
            if len(self.active_threads) >= max_threads:
                break
                
            thread_id = f"Thread-{self.thread_counter}"
            self.thread_counter += 1
            
            thread = ConversationThread(
                account, 
                self.turns_input.value(), 
                thread_id,
                delay_range,
                self.nous_model_input.text(),
                self.or_model_input.text()
            )
            
            thread.update_signal.connect(self.update_output)
            thread.progress_signal.connect(self.update_progress)
            thread.finished_signal.connect(self.thread_finished)
            thread.stats_signal.connect(self.record_response_time)
            
            self.active_threads[thread_id] = thread
            thread.start()
            
            time.sleep(0.5)  # Small delay between thread starts
        
        self.update_stats()

    def stop_all_threads(self):
        """Остановка всех потоков"""
        for thread_id, thread in list(self.active_threads.items()):
            thread.stop()
            thread.wait()
        
        self.active_threads.clear()
        self.output_area.append("\n⏹️ Все потоки остановлены")
        self.update_stats()

    def thread_finished(self, thread_id, success):
        """Завершение потока"""
        if thread_id in self.active_threads:
            del self.active_threads[thread_id]
        
        # Start next account if available
        active_accounts = [acc for acc in self.account_manager.get_active_accounts() 
                          if acc.usage_count == 0]
        
        if active_accounts and len(self.active_threads) < self.threads_input.value():
            account = active_accounts[0]
            account.usage_count = 1  # Mark as used
            
            thread_id = f"Thread-{self.thread_counter}"
            self.thread_counter += 1
            
            delay_text = self.delay_input.text()
            try:
                if "-" in delay_text:
                    min_delay, max_delay = map(float, delay_text.split("-"))
                    delay_range = (min_delay, max_delay)
                else:
                    delay = float(delay_text)
                    delay_range = (delay, delay)
            except:
                delay_range = (2, 5)
            
            thread = ConversationThread(
                account, 
                self.turns_input.value(), 
                thread_id,
                delay_range,
                self.nous_model_input.text(),
                self.or_model_input.text()
            )
            
            thread.update_signal.connect(self.update_output)
            thread.progress_signal.connect(self.update_progress)
            thread.finished_signal.connect(self.thread_finished)
            thread.stats_signal.connect(self.record_response_time)
            
            self.active_threads[thread_id] = thread
            thread.start()
        
        self.update_stats()

    def update_output(self, thread_id, message):
        """Обновление вывода"""
        self.output_area.append(f"[{thread_id}] {message}")
        self.output_area.ensureCursorVisible()

    def update_progress(self, thread_id, progress):
        """Обновление прогресса"""
        # Calculate overall progress
        total_threads = len(self.account_manager.get_active_accounts())
        if total_threads > 0:
            completed = sum(1 for acc in self.account_manager.accounts if acc.usage_count > 0)
            overall_progress = int((completed / total_threads) * 100)
            self.global_progress.setValue(overall_progress)

    def record_response_time(self, thread_id, response_time):
        """Запись времени ответа"""
        self.response_times.append(response_time)
        if len(self.response_times) > 100:
            self.response_times.pop(0)

    def update_stats(self):
        """Обновление статистики"""
        active = len(self.active_threads)
        total = len(self.account_manager.accounts)
        enabled = len(self.account_manager.get_active_accounts())
        
        self.active_threads_label.setText(f"Активных потоков: {active}")
        self.header_stats.setText(f"Аккаунты: {enabled}/{total} активны | Потоков: {active}")
        
        # Calculate success rate
        success = sum(acc.success_count for acc in self.account_manager.accounts)
        errors = sum(acc.error_count for acc in self.account_manager.accounts)
        total_attempts = success + errors
        
        if total_attempts > 0:
            success_rate = (success / total_attempts) * 100
            self.output_area.append(f"📊 Статистика: Успешно {success}/{total_attempts} ({success_rate:.1f}%)")

    def clear_accounts(self):
        """Очистка всех аккаунтов"""
        if QMessageBox.question(self, "Подтверждение", "Очистить все аккаунты?") == QMessageBox.Yes:
            self.accounts_table.setRowCount(0)
            self.account_manager.accounts.clear()
            self.update_stats()
            self.output_area.append("🗑️ Все аккаунты очищены")

    def import_prompts_from_txt(self):
        """Импорт промптов из файла"""
        options = QFileDialog.Options()
        file_name, _ = QFileDialog.getOpenFileName(
            self, "Импорт промптов", "", "Text Files (*.txt)", options=options
        )
        if file_name:
            prompts = load_prompts_from_file(file_name)
            if prompts:
                global PROMPT_DATABASE
                PROMPT_DATABASE = prompts
                self.prompt_combo.clear()
                self.prompt_combo.addItems(prompts)
                self.output_area.append(f"📥 Импортировано {len(prompts)} промптов")
            else:
                self.output_area.append("❌ Не удалось загрузить промпты из файла")

    def export_results(self):
        """Экспорт результатов"""
        options = QFileDialog.Options()
        file_name, _ = QFileDialog.getSaveFileName(
            self, "Экспорт логов", "", "Text Files (*.txt)", options=options
        )
        if file_name:
            try:
                with open(file_name, "w", encoding="utf-8") as f:
                    f.write(self.output_area.toPlainText())
                self.output_area.append(f"💾 Логи экспортированы в {file_name}")
            except Exception as e:
                self.output_area.append(f"❌ Ошибка экспорта: {str(e)}")

# =============================
# Main execution
# =============================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # Apply dark theme
    app.setStyle("Fusion")
    dark_palette = QPalette()
    dark_palette.setColor(QPalette.Window, QColor(10, 11, 18))
    dark_palette.setColor(QPalette.WindowText, Qt.white)
    dark_palette.setColor(QPalette.Base, QColor(25, 25, 35))
    dark_palette.setColor(QPalette.AlternateBase, QColor(10, 11, 18))
    dark_palette.setColor(QPalette.ToolTipBase, Qt.white)
    dark_palette.setColor(QPalette.ToolTipText, Qt.white)
    dark_palette.setColor(QPalette.Text, Qt.white)
    dark_palette.setColor(QPalette.Button, QColor(10, 11, 18))
    dark_palette.setColor(QPalette.ButtonText, Qt.white)
    dark_palette.setColor(QPalette.BrightText, Qt.red)
    dark_palette.setColor(QPalette.Link, QColor(42, 130, 218))
    dark_palette.setColor(QPalette.Highlight, QColor(123, 104, 238))
    dark_palette.setColor(QPalette.HighlightedText, Qt.black)
    app.setPalette(dark_palette)
    
    window = DeFiAIClubMassUI()
    window.show()
    sys.exit(app.exec_())