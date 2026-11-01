"""Edit a queue's printer definition (PPD) to control colour."""
import re


def gray_only(ppd):
    """Remove colour from a printer definition. A phone can ask for colour on any
    job and override a default, so colour has to be unavailable to be blocked."""
    ppd = ppd.replace("*ColorDevice: True", "*ColorDevice: False")
    ppd = ppd.replace("*DefaultColorModel: RGB", "*DefaultColorModel: Gray")
    ppd = re.sub(r"^\*(en_US\.)?ColorModel (RGB|DeviceRGB|AdobeRGB)[ /:].*\n", "", ppd, flags=re.M)
    return re.sub(r"\*APPrinterPreset [^\n]*\n(?:(?!\*End).*\n)*?\*End\n?",
                  lambda preset: "" if "ColorModel RGB" in preset.group(0) else preset.group(0), ppd)


def job_chooses(ppd):
    """Keep colour available, but print in black and white unless a job asks for colour."""
    return ppd.replace("*DefaultColorModel: RGB", "*DefaultColorModel: Gray")
