import os
import sys
import time
import shutil
import logging
import collections
from contextlib import contextmanager

from xvfbwrapper import Xvfb
from selenium import webdriver
from selenium.webdriver.firefox.firefox_binary import FirefoxBinary
from selenium.common.exceptions import (
    StaleElementReferenceException,
)
from jinja2 import Template

from ..conf import settings


logger = logging.getLogger(__name__)
PROFILE_DIR = 'firefox_profile'
HOME_PAGE = 'https://kibitzr.github.io/'


firefox_instance = {
    'xvfb_display': None,
    'driver': None,
    'headed_driver': None,
}


def cleanup():
    """Must be called before exit"""
    global firefox_instance
    temp_dirs = []
    if firefox_instance['driver'] is not None:
        if firefox_instance['driver'].profile:
            temp_dirs.append(firefox_instance['driver'].profile.profile_dir)
        firefox_instance['driver'].quit()
        firefox_instance['driver'] = None
    if firefox_instance['headed_driver'] is not None:
        if firefox_instance['headed_driver'].profile:
            temp_dirs.append(firefox_instance['headed_driver'].profile.profile_dir)
        firefox_instance['headed_driver'].quit()
        firefox_instance['headed_driver'] = None
    if firefox_instance['xvfb_display'] is not None:
        firefox_instance['xvfb_display'].stop()
        firefox_instance['xvfb_display'] = None
    for temp_dir in temp_dirs:
        shutil.rmtree(temp_dir, ignore_errors=True)


def persistent_firefox():
    if not os.path.exists(PROFILE_DIR):
        os.makedirs(PROFILE_DIR)
    with firefox(headless=False) as driver:
        driver.get(HOME_PAGE)
        while True:
            try:
                # Property raises when browser is closed:
                driver.title
            except:
                # All kinds of things happen when closing Firefox
                break
            else:
                time.sleep(0.2)
        update_profile(driver)


def update_profile(driver):
    if os.path.exists(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR)
    shutil.copytree(
        driver.profile.profile_dir,
        PROFILE_DIR,
        ignore=shutil.ignore_patterns(
            "parent.lock",
            "lock",
            ".parentlock",
            "*.sqlite-shm",
            "*.sqlite-wal",
        ),
    )


def needs_firefox(conf):
    return any(
        conf.get(key)
        for key in ('delay', 'scenario', 'form')
    )


def firefox_fetcher(conf):
    headless = conf.get('headless', True)
    with firefox(headless) as driver:
        fetcher = FirefoxFetcher(driver)
        return fetcher.fetch(conf)


class FirefoxFetcher(object):

    def __init__(self, driver):
        self.driver = driver

    def fetch(self, conf):
        """
        1. Fetch URL
        2. Run automation.
        3. Return HTML.
        4. Close the tab.
        """
        url = conf['url']
        self.driver.get(url)
        self._run_automation(conf)
        html = self._get_html()
        self._close_tab()
        return True, html

    def _run_automation(self, conf):
        """
        1. Fill form.
        2. Run scenario.
        3. Delay.
        """
        self._fill_form(self._find_form(conf))
        self._run_scenario(conf)
        self._delay(conf)

    def _fill_form(self, form):
        """
        Fill all inputs with provided Jinja2 templates.
        If no field had click key, submit last element.
        """
        clicked = False
        last_element = None
        for field in form:
            if field['text']:
                field['element'].clear()
                field['element'].send_keys(field['text'])
            if field['click']:
                field['element'].click()
                clicked = True
            last_element = field['element']
        if last_element:
            if not clicked:
                last_element.submit()

    def _find_form(self, conf):
        """
        Find elements defined in conf['form'].
        Render all Jinja2 templates from field['value'].
        Save all field['click'] triggers.
        If all found, return them as a list of dictionaries.
        If at least one was not found, return empty list.
        """
        form = conf.get('form', [])
        fields = []
        creds = settings().creds
        for field in form:
            click = field.get('click')
            text = self._parse_field_text(field, conf, creds)
            selector_type, selector = self._parse_field_selector(field)
            if selector:
                element = self._find_element(selector, selector_type)
                if element:
                    fields.append({
                        'element': element,
                        'text': text,
                        'click': click,
                    })
                else:
                    logging.warning("Element {%s: %s} not found",
                                    selector_type, selector)
        if len(fields) == len(form):
            return fields
        else:
            logging.info(
                "Skipped form filling because not all fields were found"
            )
            return []

    @staticmethod
    def _parse_field_selector(field):
        """
        Check field keys: css, xpath and id.
        Return the first found along with it's value.
        """
        for selector_type in ('css', 'xpath', 'id'):
            selector = field.get(selector_type)
            if selector:
                return selector_type, selector
        else:
            logger.warning("Form field does not define any selector "
                           "(id, css and xpath available): %r",
                           field)
            return None, None

    @staticmethod
    def _parse_field_text(field, conf, creds):
        """
        Return form field value from 3 options:
        1. If value key is present, render it as a Jinja2 template.
           The template has access to ``conf`` and ``creds`` dictionaries.
        2. If creds key is present, use it as a dot-delimited path
           in creds dictionary (e.g. ``account.login``).
        3. Otherwise return None
        """
        value = field.get('value')
        if value:
            template = Template(value)
            return template.render(
                conf=conf,
                creds=creds,
            )
        creds_path = field.get('creds')
        if creds_path:
            bread_crumbs = creds_path.split('.')
            node = creds
            for crumb in bread_crumbs:
                node = node[crumb]
            return node

    def _run_scenario(self, conf):
        scenario = conf.get('scenario')
        if isinstance(scenario, collections.Mapping):
            code = scenario['python']
            elements = scenario.get('elements', {})
        else:
            code = scenario
            elements = {}
        if elements:
            element_objects = self._find_elements(elements)
        else:
            element_objects = {}
        if code:
            self._exec_scenario(code, conf, element_objects)

    @staticmethod
    def _delay(conf):
        delay = conf.get('delay')
        if delay:
            time.sleep(delay)

    def _get_html(self):
        elem = self.driver.find_element_by_xpath("//*")
        for attempt in range(3):
            try:
                html = elem.get_attribute("outerHTML")
            except StaleElementReferenceException:
                # Crazy (but stable) race condition,
                # new page loaded after call to find_element_by_xpath
                # Just retry:
                if attempt < 2:
                    time.sleep(1)
                    elem = self.driver.find_element_by_xpath("//*")
                else:
                    raise
            else:
                return html

    def _find_elements(self, elements):
        logger.info("Finding elements")
        result = {}
        for key, selector in elements.items():
            name, _, selector_type = key.partition("|")
            result[name] = self._find_element(selector, selector_type.lower())
        return result

    def _find_element(self, selector, selector_type):
        """
        Return first matching displayed element of non-zero size
        or None if nothing found
        """
        if selector_type == 'css':
            elements = self.driver.find_elements_by_css_selector(selector)
        elif selector_type == 'xpath':
            elements = self.driver.find_elements_by_xpath_selector(selector)
        elif selector_type == 'id':
            elements = self.driver.find_elements_by_css_selector('#' + selector)
        else:
            raise RuntimeError(
                "Unknown selector_type: %s for selector: %s"
                % (selector_type, selector)
            )
        for element in elements:
            if element.is_displayed():
                if sum(element.size.values()) > 0:
                    return element

    def _exec_scenario(self, code, conf, elements):
        logger.info("Executing custom scenario")
        logger.debug(code)
        exec(
            code,
            {
                'conf': conf,
                'creds': settings().creds,
                'driver': self.driver,
                'elements': elements,
            },
        )

    def _close_tab(self):
        """
        Create a new tab and close the old one
        to avoid idle page resource usage
        """
        old_tab = self.driver.current_window_handle
        self.driver.execute_script('''window.open("about:blank", "_blank");''')
        self.driver.switch_to.window(old_tab)
        self.driver.close()
        self.driver.switch_to.window(self.driver.window_handles[0])


@contextmanager
def firefox(headless=True):
    global firefox_instance
    if headless:
        if firefox_instance['xvfb_display'] is None:
            firefox_instance['xvfb_display'] = virtual_buffer()
        driver_key = 'driver'
    else:
        driver_key = 'headed_driver'
    if firefox_instance[driver_key] is None:
        if logger.level == logging.DEBUG:
            firefox_binary = FirefoxBinary(log_file=sys.stdout)
        else:
            firefox_binary = None
        # Load profile, if it exists:
        if os.path.isdir(PROFILE_DIR):
            firefox_profile = webdriver.FirefoxProfile(PROFILE_DIR)
        else:
            firefox_profile = None
        firefox_instance[driver_key] = webdriver.Firefox(
            firefox_binary=firefox_binary,
            firefox_profile=firefox_profile,
        )
        firefox_instance[driver_key].set_window_size(1024, 768)
    yield firefox_instance[driver_key]


def virtual_buffer():
    """
    Try to start xvfb, trying multiple (up to 5) times if a failure
    """
    for _ in range(0, 6):
        xvfb_display = Xvfb()
        xvfb_display.start()
        # If Xvfb started, return.
        if xvfb_display.proc is not None:
            return xvfb_display
    raise Exception("Xvfb could not be started after six attempts.")
