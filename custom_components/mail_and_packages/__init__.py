"""Mail and Packages Integration."""
from . import const
import aiohttp
import async_timeout
import datetime
from datetime import timedelta
import email
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_RESOURCES,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import Throttle
import imageio as io
import imaplib
import logging
import os
from PIL import Image
import quopri
import re
from resizeimage import resizeimage
from shutil import copyfile
import subprocess
import uuid


_LOGGER = logging.getLogger(__name__)


async def async_setup(hass, config_entry):
    """ Disallow configuration via YAML """

    return True


async def async_setup_entry(hass, config_entry):
    """Load the saved entities."""
    _LOGGER.info(
        "Version %s is starting, if you have any issues please report" " them here: %s",
        const.VERSION,
        const.ISSUE_URL,
    )
    config_entry.options = config_entry.data

    config = config_entry.data

    data = EmailData(hass, config)

    async def async_update_data():
        """Fetch data from NUT."""
        async with async_timeout.timeout(10):
            await hass.async_add_executor_job(data.update)
            if not data:
                raise UpdateFailed("Error fetching emails")

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Mail and Packages Updater",
        update_method=async_update_data,
        update_interval=timedelta(
            minutes=config_entry.options.get(const.CONF_SCAN_INTERVAL)
        ),
    )

    # Fetch initial data so we have data when entities subscribe
    await coordinator.async_refresh()

    if const.DOMAIN_DATA not in hass.data:
        hass.data[const.DOMAIN_DATA] = {}

    hass.data[const.DOMAIN_DATA][config_entry.entry_id] = {
        const.DATA: data,
        const.COORDINATOR: coordinator,
    }

    config_entry.add_update_listener(update_listener)
    hass.async_create_task(
        hass.config_entries.async_forward_entry_setup(config_entry, const.PLATFORM)
    )

    return True


async def async_unload_entry(hass, config_entry):
    """Handle removal of an entry."""
    try:
        await hass.config_entries.async_forward_entry_unload(
            config_entry, const.PLATFORM
        )
        _LOGGER.info(
            "Successfully removed sensor from the " + const.DOMAIN + " integration"
        )
    except ValueError:
        pass
    return True


async def update_listener(hass, config_entry):
    """Update listener."""
    config_entry.data = config_entry.options
    await hass.config_entries.async_reload(config_entry.entry_id)


class EmailData:
    """The class for handling the data retrieval."""

    def __init__(self, hass, config):
        """Initialize the data object."""
        self._hass = hass
        self._config = config
        self._host = config.get(CONF_HOST)
        self._scan_interval = config.get(const.CONF_SCAN_INTERVAL)
        self._data = None

        _LOGGER.debug("Config scan interval: %s", self._scan_interval)

    def update(self):
        """Get the latest data"""
        if self._host is not None:
            """Login to email server and select the folder"""
            self._data = process_emails(self._hass, self._config)
        else:
            _LOGGER.error("Host was left blank not attempting connection")

        _LOGGER.debug("Updated scan time: %s", update_time())


def process_emails(hass, config):
    """ Process emails and return value """
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT)
    folder = config.get(const.CONF_FOLDER)
    user = config.get(CONF_USERNAME)
    pwd = config.get(CONF_PASSWORD)
    img_out_path = config.get(const.CONF_PATH)
    gif_duration = config.get(const.CONF_DURATION)
    image_security = config.get(const.CONF_IMAGE_SECURITY)
    generate_mp4 = config.get(const.CONF_GENERATE_MP4)
    resources = config.get(CONF_RESOURCES)
    amazon_fwds = config.get(const.CONF_AMAZON_FWDS)

    """Login to email server and select the folder"""
    account = login(host, port, user, pwd)

    """Do not process if account returns false"""
    if not account:
        return

    selectfolder(account, folder)

    if image_security:
        image_name = str(uuid.uuid4()) + ".gif"
    else:
        image_name = const.DEFAULT_GIF_FILE_NAME

    data = {}

    """Only update sensors we're intrested in"""
    for sensor in resources:
        count = {}
        if sensor == "usps_mail":
            count[sensor] = get_mails(
                account, img_out_path, gif_duration, image_name, generate_mp4,
            )
        elif sensor == const.AMAZON_PACKAGES:
            count[sensor] = get_items(account, const.ATTR_COUNT, amazon_fwds)
            count[const.AMAZON_ORDER] = get_items(account, const.ATTR_ORDER)
        elif sensor == const.AMAZON_HUB:
            value = amazon_hub(account, amazon_fwds)
            count[sensor] = value[const.ATTR_COUNT]
            count[const.AMAZON_HUB_CODE] = value[const.ATTR_CODE]
        elif "_packages" in sensor:
            prefix = sensor.split("_")[0]
            delivering = prefix + "_delivering"
            delivered = prefix + "_delivered"
            total = data[delivering] + data[delivered]
            count[sensor] = total
        elif "_delivering" in sensor:
            prefix = sensor.split("_")[0]
            delivering = prefix + "_delivering"
            delivered = prefix + "_delivered"
            tracking = prefix + "_tracking"
            info = get_count(account, sensor, True)
            total = info[const.ATTR_COUNT] - data[delivered]
            total = max(0, total)
            count[sensor] = total
            count[tracking] = info[const.ATTR_TRACKING]
        elif sensor == "zpackages_delivered":
            count[sensor] = 0  # initialize the variable
            for shipper in const.SHIPPERS:
                delivered = shipper + "_delivered"
                if delivered in data and delivered != sensor:
                    count[sensor] += data[delivered]
        elif sensor == "zpackages_transit":
            total = 0
            for shipper in const.SHIPPERS:
                delivering = shipper + "_delivering"
                if delivering in data and delivering != sensor:
                    total += data[delivering]
            count[sensor] = max(0, total)
        elif sensor == "mail_updated":
            count[sensor] = update_time()
        else:
            count[sensor] = get_count(account, sensor, False, img_out_path, hass)[
                const.ATTR_COUNT
            ]

        data.update(count)

    return data


def login(host, port, user, pwd):
    """function used to login"""

    """Catch invalid mail server / host names"""
    try:
        account = imaplib.IMAP4_SSL(host, port)

    except imaplib.IMAP4.error as err:
        _LOGGER.error("Error connecting into IMAP Server: %s", str(err))
        return False
    except Exception as err:
        _LOGGER.error("Network error while connecting to server: %s", str(err))
        return False

    """If login fails give error message"""
    try:
        rv, data = account.login(user, pwd)
    except imaplib.IMAP4.error as err:
        _LOGGER.error("Error logging into IMAP Server: %s", str(err))
    return account


def selectfolder(account, folder):
    """Select folder inside the mailbox"""
    try:
        rv, mailboxes = account.list()
    except imaplib.IMAP4.error as err:
        _LOGGER.error("Error listing folders: %s", str(err))
    try:
        rv, data = account.select(folder)
    except imaplib.IMAP4.error as err:
        _LOGGER.error("Error selecting folder: %s", str(err))


def get_formatted_date():
    """Returns today in specific format"""
    today = datetime.datetime.today().strftime("%d-%b-%Y")
    """
    # for testing
    # today = '06-May-2020'
    """
    return today


def update_time():
    """gets update time"""
    updated = datetime.datetime.now().strftime("%b-%d-%Y %I:%M %p")

    return updated


def email_search(account, address, date, subject=None):
    """ Search emails with from, subject, senton date.

    Returns a tuple
    """

    imap_search = None  # Holds our IMAP SEARCH command

    if isinstance(address, list) and subject is not None:
        email_list = '" OR FROM "'.join(address)
        imap_search = f'(FROM "{email_list}" SUBJECT "{subject}" SENTON "{date}")'

    elif subject is not None:
        imap_search = f'(FROM "{address}" SUBJECT "{subject}" SENTON "{date}")'
    else:
        imap_search = f'(FROM "{address}" SENTON "{date}")'

    try:
        value = account.search(None, imap_search)
    except imaplib.IMAP4.error as err:
        _LOGGER.error("Error searching emails: %s", str(err))
        return "BAD", str(err)

    return value


def email_fetch(account, num, type):
    """ Download specified email for parsing.

    Returns tuple
    """
    value = account.fetch(num, "(RFC822)")

    return value


def get_mails(account, image_output_path, gif_duration, image_name, gen_mp4=False):
    """Creates GIF image based on the attachments in the inbox"""
    today = get_formatted_date()
    image_count = 0
    images = []
    imagesDelete = []
    msg = ""

    _LOGGER.debug("Attempting to find Informed Delivery mail")

    (rv, data) = email_search(
        account, const.USPS_Mail_Email, today, const.USPS_Mail_Subject
    )

    # Check to see if the path exists, if not make it
    pathcheck = os.path.isdir(image_output_path)
    if not pathcheck:
        try:
            os.makedirs(image_output_path)
        except Exception as err:
            _LOGGER.critical("Error creating directory: %s", str(err))

    """Clean up image directory"""
    _LOGGER.debug("Cleaning up image directory: %s", str(image_output_path))
    cleanup_images(image_output_path)

    if rv == "OK":
        _LOGGER.debug("Informed Delivery email found processing...")
        for num in data[0].split():
            (rv, data) = email_fetch(account, num, "(RFC822)")
            msg = email.message_from_string(data[0][1].decode("utf-8"))

            """walking through the email parts to find images"""
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get("Content-Disposition") is None:
                    continue

                _LOGGER.debug("Extracting image from email")
                filepath = image_output_path + part.get_filename()

                """Log error message if we are unable to open the filepath for
                some reason"""
                try:
                    fp = open(filepath, "wb")
                except Exception as err:
                    _LOGGER.critical("Error opening filepath: %s", str(err))
                fp.write(part.get_payload(decode=True))
                images.append(filepath)
                image_count = image_count + 1
                fp.close()

        """Remove duplicate images"""
        _LOGGER.debug("Removing duplicate images.")
        images = list(dict.fromkeys(images))

        """Create copy of image list for deleting temporary images"""
        imagesDelete = images[:]

        """Look for mail pieces without images image"""
        html_text = str(msg)
        link_pattern = re.compile(r"\bimage-no-mailpieces?700\.jpg\b")
        search = link_pattern.search(html_text)
        if search is not None:
            images.append(os.path.dirname(__file__) + "/image-no-mailpieces700.jpg")
            image_count = image_count + 1
            _LOGGER.debug(
                "Placeholder image found using: " + "image-no-mailpieces700.jpg."
            )

        """Remove USPS announcement images"""
        _LOGGER.debug("Removing USPS announcement images.")
        remove_terms = ["mailerProvidedImage", "ra_0", "Mail Attachment.txt"]
        images = [
            el for el in images if not any(ignore in el for ignore in remove_terms)
        ]
        image_count = len(images)
        _LOGGER.debug("Image Count: %s", str(image_count))

        if image_count > 0:
            all_images = []

            _LOGGER.debug("Resizing images to 724x320...")
            """Resize images to 724x320"""
            all_images = resize_images(images, 724, 320)

            """Create copy of image list for deleting temporary images"""
            for image in all_images:
                imagesDelete.append(image)

            """Create numpy array of images"""
            _LOGGER.debug("Creating array of image files...")
            all_images = [io.imread(image) for image in all_images]

            try:
                _LOGGER.debug("Generating animated GIF")
                """Use ImageIO to create mail images"""
                io.mimwrite(
                    os.path.join(image_output_path, image_name),
                    all_images,
                    duration=gif_duration,
                )
                _LOGGER.info("Mail image generated.")
            except Exception as err:
                _LOGGER.error("Error attempting to generate image: %s", str(err))
            for image in imagesDelete:
                try:
                    os.remove(image)
                except Exception as err:
                    _LOGGER.error("Error attempting to remove image: %s", str(err))

        elif image_count == 0:
            _LOGGER.info("No mail found.")
            filecheck = os.path.isfile(image_output_path + image_name)
            if filecheck:
                try:
                    _LOGGER.debug("Removing " + image_output_path + image_name)
                    os.remove(image_output_path + image_name)
                except Exception as err:
                    _LOGGER.error("Error attempting to remove image: %s", str(err))
            try:
                _LOGGER.debug("Copying nomail gif")
                copyfile(
                    os.path.dirname(__file__) + "/mail_none.gif",
                    image_output_path + image_name,
                )
            except Exception as err:
                _LOGGER.error("Error attempting to copy image: %s", str(err))

        if gen_mp4:
            _generate_mp4(image_output_path, image_name)

    return image_count


def _generate_mp4(path, image_file):
    """
    Generate mp4 from gif
    use a subprocess so we don't lock up the thread
    comamnd: ffmpeg -f gif -i infile.gif outfile.mp4
    """
    gif_image = os.path.join(path, image_file)
    mp4_file = os.path.join(path, image_file.replace(".gif", ".mp4"))
    filecheck = os.path.isfile(mp4_file)
    _LOGGER.debug("Generating mp4: %s", mp4_file)
    if filecheck:
        try:
            os.remove(mp4_file)
            _LOGGER.debug("Removing old mp4: %s", mp4_file)
        except Exception as err:
            _LOGGER.error("Error attempting to remove mp4: %s", str(err))

    subprocess.call(
        [
            "ffmpeg",
            "-f",
            "gif",
            "-i",
            gif_image,
            "-pix_fmt",
            "yuv420p",
            "-filter:v",
            "crop='floor(in_w/2)*2:floor(in_h/2)*2'",
            mp4_file,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def resize_images(images, width, height):
    """
    Resize images
    This should keep the aspect ratio of the images
    """
    all_images = []
    for image in images:
        try:
            fd_img = open(image, "rb")
        except Exception as err:
            _LOGGER.error("Error attempting to open image %s: %s", str(image), str(err))
            continue
        try:
            img = Image.open(fd_img)
        except Exception as err:
            _LOGGER.error("Error attempting to read image %s: %s", str(image), str(err))
            continue
        img = resizeimage.resize_contain(img, [width, height])
        pre, ext = os.path.splitext(image)
        image = pre + ".gif"
        img.save(image, img.format)
        fd_img.close()
        all_images.append(image)

    return all_images


def cleanup_images(path):
    """
    Clean up image storage directory
    Only supose to delete .gif and .mp4 files
    """
    for file in os.listdir(path):
        if file.endswith(".gif") or file.endswith(".mp4"):
            os.remove(path + file)


def get_count(account, sensor_type, get_tracking_num=False, image_path=None, hass=None):
    """
    Get Package Count
    add IF clauses to filter by sensor_type for email and subjects
    todo: convert subjects to list and use a for loop
    """
    count = 0
    tracking = []
    result = {}
    today = get_formatted_date()
    email = None
    subject = None
    subject_2 = None
    filter_text = None
    shipper = None

    if sensor_type == const.USPS_DELIVERED:
        email = const.USPS_Packages_Email
        subject = const.USPS_Delivered_Subject
    elif sensor_type == const.USPS_DELIVERING:
        email = const.USPS_Packages_Email
        subject = const.USPS_Delivering_Subject
        filter_text = const.USPS_Body_Text
        if get_tracking_num:
            shipper = const.SHIPPERS[4]
    elif sensor_type == const.UPS_DELIVERED:
        email = const.UPS_Email
        subject = const.UPS_Delivered_Subject
        filter_text = const.UPS_Body_Text
    elif sensor_type == const.UPS_DELIVERING:
        email = const.UPS_Email
        subject = const.UPS_Delivering_Subject
        subject_2 = const.UPS_Delivering_Subject_2
        if get_tracking_num:
            shipper = const.SHIPPERS[3]
    elif sensor_type == const.FEDEX_DELIVERING:
        email = const.FEDEX_Email
        subject = const.FEDEX_Delivering_Subject
        subject_2 = const.FEDEX_Delivering_Subject_2
        if get_tracking_num:
            shipper = const.SHIPPERS[2]
    elif sensor_type == const.DHL_DELIVERING:
        email = const.DHL_Email
        subject = const.DHL_Delivering_Subject
        filter_text = const.DHL_Body_Text_2
        if get_tracking_num:
            shipper = const.SHIPPERS[1]
    elif sensor_type == const.FEDEX_DELIVERED:
        email = const.FEDEX_Email
        subject = const.FEDEX_Delivered_Subject
    elif sensor_type == const.CAPOST_DELIVERED:
        email = const.CAPost_Email
        subject = const.CAPost_Delivered_Subject
    elif sensor_type == const.DHL_DELIVERED:
        email = const.DHL_Email
        subject = const.DHL_Delivered_Subject
        filter_text = const.DHL_Body_Text
    elif sensor_type == const.AMAZON_DELIVERED:
        result[const.ATTR_COUNT] = amazon_search(account, image_path, hass)
        result[const.ATTR_TRACKING] = ""
        return result
    else:
        _LOGGER.debug("Unknown sensor type: %s", str(sensor_type))
        result[const.ATTR_COUNT] = count
        result[const.ATTR_TRACKING] = ""
        return result

    _LOGGER.debug(
        "Attempting to find mail from (%s) with subject 1 (%s)", email, subject
    )

    (rv, data) = email_search(account, email, today, subject)

    if rv == "OK":
        if filter_text is not None:
            count += find_text(data[0], account, filter_text)
        else:
            count += len(data[0].split())
        _LOGGER.debug(
            "Search for (%s) with subject 1 (%s) results: %s count: %s",
            email,
            subject,
            data[0],
            count,
        )
        if shipper is not None and count > 0:
            tracking = get_tracking(data[0], account, shipper)

    if subject_2 is not None:
        _LOGGER.debug(
            "Attempting to find mail from (%s) with subject 2 (%s)", email, subject_2
        )

        (rv, data) = email_search(account, email, today, subject_2)

        if rv == "OK":
            if filter_text is not None:
                count += find_text(data[0], account, filter_text)
            else:
                count += len(data[0].split())
            _LOGGER.debug(
                "Search for (%s) with subject 2 (%s) results: %s count: %s",
                email,
                subject_2,
                data[0],
                count,
            )
            if shipper is not None and count > 0:
                tracking = get_tracking(data[0], account, shipper)

    if tracking:
        # Try to guard against duplicate emails via tracking number
        if len(tracking) < count:
            count = len(tracking)

    result[const.ATTR_TRACKING] = tracking

    result[const.ATTR_COUNT] = count
    return result


def get_tracking(sdata, account, shipper):
    """Parse tracking numbers from email subject lines"""
    _LOGGER.debug("Searching for tracking numbers for (%s)", shipper)
    tracking = []
    pattern = None
    mail_list = sdata.split()

    if shipper == "usps":
        pattern = re.compile(r"{}".format(const.USPS_TRACKING_PATTERN))
    elif shipper == "ups":
        pattern = re.compile(r"{}".format(const.UPS_TRACKING_PATTERN))
    elif shipper == "fedex":
        pattern = re.compile(r"{}".format(const.FEDEX_TRACKING_PATTERN))
    elif shipper == "dhl":
        pattern = re.compile(r"{}".format(const.DHL_TRACKING_PATTERN))

    for i in mail_list:
        typ, data = email_fetch(account, i, "(RFC822)")
        for response_part in data:
            if not isinstance(response_part, tuple):
                continue
            msg = email.message_from_bytes(response_part[1])

            # Search subject for a tracking number
            email_subject = msg["subject"]
            found = pattern.findall(email_subject)
            if len(found) > 0:
                _LOGGER.debug(
                    "Found (%s) tracking number in email subject: (%s)",
                    shipper,
                    found[0],
                )
                if found[0] in tracking:
                    continue
                tracking.append(found[0])
                continue

            # Search in email body for tracking number
            email_msg = quopri.decodestring(str(msg.get_payload(0)))
            email_msg = email_msg.decode("utf-8")
            found = pattern.findall(email_msg)
            if len(found) > 0:
                _LOGGER.debug(
                    "Found (%s) tracking number in email body: %s", shipper, found[0]
                )
                if found[0] in tracking:
                    continue
                tracking.append(found[0])
                continue

    if len(tracking) == 0:
        _LOGGER.debug("No tracking number found for %s", shipper)

    return tracking


def find_text(sdata, account, search):
    """
    Filter for specific words in email
    Return count of items found
    """
    _LOGGER.debug("Searching for (%s) in (%s) emails", search, len(sdata))
    mail_list = sdata.split()
    count = 0
    found = None

    for i in mail_list:
        typ, data = email_fetch(account, i, "(RFC822)")
        for response_part in data:
            if not isinstance(response_part, tuple):
                continue
            msg = email.message_from_bytes(response_part[1])
            email_msg = quopri.decodestring(str(msg.get_payload(0)))
            try:
                email_msg = email_msg.decode("utf-8")
            except Exception as err:
                _LOGGER.warning(
                    "Error while attempting to find (%s) in email: %s",
                    search,
                    str(err),
                )
                continue
            pattern = re.compile(r"{}".format(search))
            found = pattern.search(email_msg)
            if found is not None:
                _LOGGER.debug("Found (%s) in email", search)
                count += 1

    _LOGGER.debug("Search for (%s) count results: %s", search, count)
    return count


def amazon_search(account, image_path, hass):
    """ Find Amazon Delivered email """
    _LOGGER.debug("Searching for Amazon delivered email(s)...")
    domains = const.Amazon_Domains.split(",")
    subject = const.AMAZON_Delivered_Subject
    today = get_formatted_date()
    count = 0

    for domain in domains:
        email = const.AMAZON_Email + domain
        (rv, data) = email_search(account, email, today, subject)

        if rv != "OK":
            continue

        count += len(data[0].split())
        _LOGGER.debug("Amazon delivered email(s) found: %s", count)
        get_amazon_image(data[0], account, image_path, hass)

    return count


def get_amazon_image(sdata, account, image_path, hass):
    """ Find Amazon delivery image """
    _LOGGER.debug("Searching for Amazon image in emails...")
    search = const.AMAZON_IMG_PATTERN

    img_url = None
    mail_list = sdata.split()
    _LOGGER.debug("HTML Amazon emails found: %s", len(mail_list))

    for i in mail_list:
        typ, data = email_fetch(account, i, "(RFC822)")
        for response_part in data:
            if not isinstance(response_part, tuple):
                continue
            msg = email.message_from_bytes(response_part[1])
            _LOGGER.debug("Email Multipart: %s", str(msg.is_multipart()))
            _LOGGER.debug("Content Type: %s", str(msg.get_content_type()))
            if not msg.is_multipart() and msg.get_content_type() != "text/html":
                continue
            for part in msg.walk():
                if part.get_content_type() != "text/html":
                    continue
                _LOGGER.debug("Processing HTML email...")
                body = part.get_payload(decode=True)
                body = body.decode("utf-8")
                pattern = re.compile(r"{}".format(search))
                found = pattern.findall(body)
                for url in found:
                    if url[1] != "us-prod-temp.s3.amazonaws.com":
                        continue
                    img_url = url[0] + url[1] + url[2]
                    _LOGGER.debug("Amazon img URL: %s", img_url)
                    break

    if img_url is not None:
        """ Download the image we found """
        hass.add_job(download_img(img_url, image_path))


async def download_img(img_url, img_path):
    """ Download image from url """
    filepath = img_path + "amazon_delivered.jpg"
    async with aiohttp.ClientSession() as session:
        async with session.get(img_url.replace("&amp;", "&")) as resp:
            if resp.status != 200:
                _LOGGER.error("Problem downloading file http error: %s", resp.status)
                return
            content_type = resp.headers["content-type"]
            _LOGGER.debug("URL content-type: %s", content_type)
            if "image" in content_type:
                data = await resp.read()
                _LOGGER.debug("Downloading image to: %s", filepath)
                with open(filepath, "wb") as f:
                    f.write(data)
                    _LOGGER.debug("Amazon image downloaded")


def amazon_hub(account, fwds=None):
    """ Find Amazon Hub info and return it """
    email_address = const.AMAZON_HUB_EMAIL
    subject_regex = const.AMAZON_HUB_SUBJECT
    info = {}
    past_date = datetime.date.today() - datetime.timedelta(days=3)
    tfmt = past_date.strftime("%d-%b-%Y")

    try:
        (rv, sdata) = account.search(
            None, '(FROM "' + email_address + '" SINCE ' + tfmt + ")"
        )
    except imaplib.IMAP4.error as err:
        _LOGGER.error("Error searching emails: %s", str(err))

    else:
        found = []
        mail_ids = sdata[0]
        id_list = mail_ids.split()
        _LOGGER.debug("Amazon hub emails found: %s", str(len(id_list)))
        for i in id_list:
            typ, data = account.fetch(i, "(RFC822)")
            for response_part in data:
                if not isinstance(response_part, tuple):
                    continue
                msg = email.message_from_bytes(response_part[1])

                # Get combo number from subject line
                email_subject = msg["subject"]
                re.compile(r"{}".format(subject_regex))
                found.append(pattern.findall(email_subject))

        info[ATTR_COUNT] = len(found)
        info[ATTR_CODE] = found
    return info


def get_items(account, param, fwds=None):
    """Parse Amazon emails for delivery date and order number"""

    _LOGGER.debug("Attempting to find Amazon email with item list ...")

    # Limit to past 3 days (plan to make this configurable)
    past_date = datetime.date.today() - datetime.timedelta(days=3)
    tfmt = past_date.strftime("%d-%b-%Y")
    deliveriesToday = []
    orderNum = []
    domains = const.Amazon_Domains.split(",")
    if fwds and fwds != ['""']:
        for fwd in fwds:
            domains.append(fwd)

    for domain in domains:
        if "@" in domain:
            email_address = domain
        else:
            email_address = "shipment-tracking@" + domain

        (rv, sdata) = email_search(account, email_address, tfmt)

        if rv == "OK":
            mail_ids = sdata[0]
            id_list = mail_ids.split()
            _LOGGER.debug("Amazon emails found: %s", str(len(id_list)))
            for i in id_list:
                typ, data = email_fetch(account, i, "(RFC822)")
                for response_part in data:
                    if not isinstance(response_part, tuple):
                        continue
                    msg = email.message_from_bytes(response_part[1])

                    # Get order number from subject line
                    email_subject = msg["subject"]
                    pattern = re.compile(r"#[0-9]{3}-[0-9]{7}-[0-9]{7}")
                    found = pattern.findall(email_subject)

                    # Don't add the same order number twice
                    if len(found) > 0 and found[0] not in orderNum:
                        orderNum.append(found[0])

                    # Catch bad format emails
                    try:
                        email_msg = quopri.decodestring(str(msg.get_payload(0)))
                        email_msg = email_msg.decode("utf-8")
                    except Exception as err:
                        _LOGGER.debug(
                            "Error while attempting to parse Amazon email: %s",
                            str(err),
                        )
                        continue

                    if "will arrive:" in email_msg:
                        start = email_msg.find("will arrive:") + len("will arrive:")
                        end = email_msg.find("Track your package:")
                        arrive_date = email_msg[start:end].strip()
                        arrive_date = arrive_date.split(" ")
                        arrive_date = arrive_date[0:3]
                        arrive_date[2] = arrive_date[2][:2]
                        arrive_date = " ".join(arrive_date).strip()
                        dateobj = datetime.datetime.strptime(arrive_date, "%A, %B %d")
                        if (
                            dateobj.day == datetime.date.today().day
                            and dateobj.month == datetime.date.today().month
                        ):
                            deliveriesToday.append("Amazon Order")

                    elif "estimated delivery date is:" in email_msg:
                        start = email_msg.find("estimated delivery date is:") + len(
                            "estimated delivery date is:"
                        )
                        end = email_msg.find("Track your package at")
                        arrive_date = email_msg[start:end].strip()
                        arrive_date = arrive_date.split(" ")
                        arrive_date = arrive_date[0:3]
                        arrive_date[2] = arrive_date[2][:2]
                        arrive_date = " ".join(arrive_date).strip()
                        dateobj = datetime.datetime.strptime(arrive_date, "%A, %B %d")
                        if (
                            dateobj.day == datetime.date.today().day
                            and dateobj.month == datetime.date.today().month
                        ):
                            deliveriesToday.append("Amazon Order")

                    # Amazon Canda language ¯\_(ツ)_/¯
                    elif "guaranteed delivery date is:" in email_msg:
                        start = email_msg.find("guaranteed delivery date is:") + len(
                            "guaranteed delivery date is:"
                        )
                        end = email_msg.find("Track your package at")
                        arrive_date = email_msg[start:end].strip()
                        arrive_date = arrive_date.split(" ")
                        arrive_date = arrive_date[0:3]
                        arrive_date[2] = arrive_date[2][:2]
                        arrive_date = " ".join(arrive_date).strip()
                        dateobj = datetime.datetime.strptime(arrive_date, "%A, %B %d")
                        if (
                            dateobj.day == datetime.date.today().day
                            and dateobj.month == datetime.date.today().month
                        ):
                            deliveriesToday.append("Amazon Order")

    if param == "count":
        _LOGGER.debug("Amazon Count: %s", str(len(deliveriesToday)))
        return len(deliveriesToday)
    elif param == "order":
        _LOGGER.debug("Amazon order: %s", str(orderNum))
        return orderNum
    else:
        _LOGGER.debug("Amazon json: %s", str(deliveriesToday))
        return deliveriesToday
