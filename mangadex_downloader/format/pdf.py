# MIT License

# Copyright (c) 2022 Rahman Yusuf

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import logging
import io
import os
import time
import shutil

from pathvalidate import sanitize_filename
from tqdm import tqdm
from .base import BaseFormat
from .utils import (
    NumberWithLeadingZeros,
    get_chapter_info,
)
from ..errors import PillowNotInstalled
from ..utils import create_directory, delete_file

log = logging.getLogger(__name__)

try:
    from PIL import (
        Image,
        ImageFile,
        ImageSequence,
        PdfParser,
        __version__
    )
except ImportError:
    pillow_ready = False
else:
    pillow_ready = True

class _PageRef:
    def __init__(self, func, *args, **kwargs):
        self._func = func
        self._args = args
        self._kwargs = kwargs
    
    def __call__(self):
        return self._func(*self._args, **self._kwargs)

class PDFPlugin:
    def __init__(self, ims):
        # "Circular Imports" problem
        from ..config import config

        self.tqdm = tqdm(
            desc='pdf_progress',
            total=len(ims),
            initial=0,
            unit='item',
            disable=config.no_progress_bar
        )

        self.register_pdf_handler()

    def check_truncated(self, img):
        # Pillow won't load truncated images
        # See https://github.com/python-pillow/Pillow/issues/1510
        # Image reference: https://mangadex.org/chapter/1615adcb-5167-4459-8b12-ee7cfbdb10d9/16
        err = None
        try:
            img.load()
        except OSError as e:
            err = e
        else:
            return False
        
        if err and 'broken data stream' in str(err):
            ImageFile.LOAD_TRUNCATED_IMAGES = True
        elif err:
            # Other error
            raise err
        
        # Load it again
        img.load()

        return True

    def _save_all(self, im, fp, filename):
        self._save(im, fp, filename, save_all=True)

    # This was modified version of Pillow/PdfImagePlugin.py version 9.0.1
    # The images will be automatically converted to RGB and closed when done converting to PDF
    def _save(self, im, fp, filename, save_all=False):
        is_appending = im.encoderinfo.get("append", False)
        if is_appending:
            existing_pdf = PdfParser.PdfParser(f=fp, filename=filename, mode="r+b")
        else:
            existing_pdf = PdfParser.PdfParser(f=fp, filename=filename, mode="w+b")

        resolution = im.encoderinfo.get("resolution", 72.0)

        info = {
            "title": None
            if is_appending
            else os.path.splitext(os.path.basename(filename))[0],
            "author": None,
            "subject": None,
            "keywords": None,
            "creator": None,
            "producer": None,
            "creationDate": None if is_appending else time.gmtime(),
            "modDate": None if is_appending else time.gmtime(),
        }
        for k, default in info.items():
            v = im.encoderinfo.get(k) if k in im.encoderinfo else default
            if v:
                existing_pdf.info[k[0].upper() + k[1:]] = v

        #
        # make sure image data is available
        im.load()

        existing_pdf.start_writing()
        existing_pdf.write_header()
        existing_pdf.write_comment(f"created by Pillow {__version__} PDF driver")

        #
        # pages
        encoderinfo = im.encoderinfo.copy()
        ims = [im]
        if save_all:
            append_images = im.encoderinfo.get("append_images", [])
            ims.extend(append_images)

        numberOfPages = 0
        image_refs = []
        page_refs = []
        contents_refs = []
        for im_ref in ims:
            img = im_ref() if isinstance(im_ref, _PageRef) else im_ref
            im_numberOfPages = 1
            if save_all:
                try:
                    im_numberOfPages = img.n_frames
                except AttributeError:
                    # Image format does not have n_frames.
                    # It is a single frame image
                    pass
            numberOfPages += im_numberOfPages
            for i in range(im_numberOfPages):
                image_refs.append(existing_pdf.next_object_id(0))
                page_refs.append(existing_pdf.next_object_id(0))
                contents_refs.append(existing_pdf.next_object_id(0))
                existing_pdf.pages.append(page_refs[-1])
            
            # Reduce Opened files
            if isinstance(im_ref, _PageRef):
                img.close()

        #
        # catalog and list of pages
        existing_pdf.write_catalog()

        if ImageFile.LOAD_TRUNCATED_IMAGES:
            ImageFile.LOAD_TRUNCATED_IMAGES = False

        pageNumber = 0
        for im_ref in ims:
            im = im_ref() if isinstance(im_ref, _PageRef) else im_ref

            truncated = self.check_truncated(im)

            if im.mode != 'RGB':
                # Convert to RGB mode
                imSequence = im.convert('RGB')

                # Close image to save memory
                im.close()
            else:
                # Already in RGB mode
                imSequence = im

            # Copy necessary encoderinfo to new image
            imSequence.encoderinfo = encoderinfo.copy()

            im_pages = ImageSequence.Iterator(imSequence) if save_all else [imSequence]
            for im in im_pages:
                # FIXME: Should replace ASCIIHexDecode with RunLengthDecode
                # (packbits) or LZWDecode (tiff/lzw compression).  Note that
                # PDF 1.2 also supports Flatedecode (zip compression).

                bits = 8
                params = None
                decode = None

                if im.mode == "1":
                    filter = "DCTDecode"
                    colorspace = PdfParser.PdfName("DeviceGray")
                    procset = "ImageB"  # grayscale
                    bits = 1
                elif im.mode == "L":
                    filter = "DCTDecode"
                    # params = f"<< /Predictor 15 /Columns {width-2} >>"
                    colorspace = PdfParser.PdfName("DeviceGray")
                    procset = "ImageB"  # grayscale
                elif im.mode == "P":
                    filter = "ASCIIHexDecode"
                    palette = im.getpalette()
                    colorspace = [
                        PdfParser.PdfName("Indexed"),
                        PdfParser.PdfName("DeviceRGB"),
                        255,
                        PdfParser.PdfBinary(palette),
                    ]
                    procset = "ImageI"  # indexed color
                elif im.mode == "RGB":
                    filter = "DCTDecode"
                    colorspace = PdfParser.PdfName("DeviceRGB")
                    procset = "ImageC"  # color images
                elif im.mode == "CMYK":
                    filter = "DCTDecode"
                    colorspace = PdfParser.PdfName("DeviceCMYK")
                    procset = "ImageC"  # color images
                    decode = [1, 0, 1, 0, 1, 0, 1, 0]
                else:
                    raise ValueError(f"cannot save mode {im.mode}")

                #
                # image

                op = io.BytesIO()

                if filter == "ASCIIHexDecode":
                    ImageFile._save(im, op, [("hex", (0, 0) + im.size, 0, im.mode)])
                elif filter == "DCTDecode":
                    Image.SAVE["JPEG"](im, op, filename)
                elif filter == "FlateDecode":
                    ImageFile._save(im, op, [("zip", (0, 0) + im.size, 0, im.mode)])
                elif filter == "RunLengthDecode":
                    ImageFile._save(im, op, [("packbits", (0, 0) + im.size, 0, im.mode)])
                else:
                    raise ValueError(f"unsupported PDF filter ({filter})")

                #
                # Get image characteristics

                width, height = im.size

                existing_pdf.write_obj(
                    image_refs[pageNumber],
                    stream=op.getvalue(),
                    Type=PdfParser.PdfName("XObject"),
                    Subtype=PdfParser.PdfName("Image"),
                    Width=width,  # * 72.0 / resolution,
                    Height=height,  # * 72.0 / resolution,
                    Filter=PdfParser.PdfName(filter),
                    BitsPerComponent=bits,
                    Decode=decode,
                    DecodeParams=params,
                    ColorSpace=colorspace,
                )

                #
                # page

                existing_pdf.write_page(
                    page_refs[pageNumber],
                    Resources=PdfParser.PdfDict(
                        ProcSet=[PdfParser.PdfName("PDF"), PdfParser.PdfName(procset)],
                        XObject=PdfParser.PdfDict(image=image_refs[pageNumber]),
                    ),
                    MediaBox=[
                        0,
                        0,
                        width * 72.0 / resolution,
                        height * 72.0 / resolution,
                    ],
                    Contents=contents_refs[pageNumber],
                )

                #
                # page contents

                page_contents = b"q %f 0 0 %f 0 0 cm /image Do Q\n" % (
                    width * 72.0 / resolution,
                    height * 72.0 / resolution,
                )

                existing_pdf.write_obj(contents_refs[pageNumber], stream=page_contents)

                self.tqdm.update(1)
                pageNumber += 1
            
            # Close image to save memory
            imSequence.close()

            # For security sake
            if truncated:
                ImageFile.LOAD_TRUNCATED_IMAGES = False

        #
        # trailer
        existing_pdf.write_xref_and_trailer()
        if hasattr(fp, "flush"):
            fp.flush()
        existing_pdf.close()

    def close_progress_bar(self):
        self.tqdm.close()

    def register_pdf_handler(self):
        Image.init()

        Image.register_save('PDF', self._save)
        Image.register_save_all('PDF', self._save_all)
        Image.register_extension('PDF', '.pdf')

        Image.register_mime("PDF", "application/pdf")

class PDF(BaseFormat):
    def __init__(self, *args, **kwargs):
        if not pillow_ready:
            raise PillowNotInstalled("pillow is not installed")

        super().__init__(*args, **kwargs)

    def convert(self, imgs, target):
        pdf_plugin = PDFPlugin(imgs)

        # Because images from BaseFormat.get_images() was just bunch of pathlib.Path
        # objects, we need convert it to _PageRef for be able Modified Pillow can convert it
        images = []
        for im in imgs:
            images.append(_PageRef(Image.open, im))
        
        im_ref = images.pop(0)
        im = im_ref()

        pdf_plugin.check_truncated(im)

        im.save(
            target,
            save_all=True,
            append_images=images
        )

        pdf_plugin.close_progress_bar()

    def main(self):
        manga = self.manga
        worker = self.create_worker()

        # Begin downloading
        for chap_class, chap_images in manga.chapters.iter(**self.kwargs_iter):
            chap_name = chap_class.get_simplified_name()
            count = NumberWithLeadingZeros(0)

            pdf_file = self.path / (chap_name + '.pdf')
            if pdf_file.exists():

                if self.replace:
                    delete_file(pdf_file)
                else:
                    log.info(f"'{pdf_file.name}' is exist and replace is False, cancelling download...")
                    continue

            chapter_path = create_directory(chap_name, self.path)

            images = self.get_images(chap_class, chap_images, chapter_path, count)
            log.info(f"{chap_name} has finished download, converting to pdf...")

            # Save it as pdf
            worker.submit(lambda: self.convert(images, pdf_file))

            # Remove original chapter folder
            shutil.rmtree(chapter_path, ignore_errors=True)

        # Shutdown queue-based thread process
        worker.shutdown()

class PDFSingle(PDF):
    def main(self):
        manga = self.manga
        worker = self.create_worker()
        images = []
        count = NumberWithLeadingZeros(0)

        result_cache = self.get_fmt_single_cache(manga)

        if result_cache is None:
            # The chapters is empty
            # there is nothing we can download
            worker.shutdown()
            return
        
        cache, _, merged_name = result_cache
        pdf_file = self.path / (merged_name + '.pdf')

        if pdf_file.exists():
            if self.replace:
                delete_file(pdf_file)
            else:
                log.info(f"'{pdf_file.name}' is exist and replace is False, cancelling download...")
                return

        path = create_directory(merged_name, self.path)

        for chap_class, chap_images in cache:
            # Insert "start of the chapter" image
            img_name = count.get() + '.png'
            img_path = path / img_name

            if not self.no_chapter_info:
                get_chapter_info(chap_class, img_path, self.replace)
                images.append(img_path)
                count.increase()

            images.extend(self.get_images(chap_class, chap_images, path, count))

        log.info("Manga \"%s\" has finished download, converting to pdf..." % manga.title)

        # Save it as pdf
        worker.submit(lambda: self.convert(images, pdf_file))

        # Remove downloaded images
        shutil.rmtree(path, ignore_errors=True)

        # Shutdown queue-based thread process
        worker.shutdown()

class PDFVolume(PDF):
    def main(self):
        manga = self.manga
        worker = self.create_worker()

        # Sorting volumes
        log.info("Preparing to download")
        cache = {}
        def append_cache(volume, item):
            try:
                data = cache[volume]
            except KeyError:
                cache[volume] = [item]
            else:
                data.append(item)

        kwargs_iter = self.kwargs_iter.copy()
        kwargs_iter['log_cache'] = True
        for chap_class, chap_images in manga.chapters.iter(**kwargs_iter):
            append_cache(chap_class.volume, [chap_class, chap_images])

        # Begin downloading
        for volume, chapters in cache.items():
            images = []
            count = NumberWithLeadingZeros(0)

            # Build volume folder name
            if volume is not None:
                vol_name = f'Vol. {volume}'
            else:
                vol_name = 'No Volume'

            pdf_name = vol_name + '.pdf'
            pdf_file = self.path / pdf_name

            if pdf_file.exists():
                if self.replace:
                    delete_file(pdf_file)
                else:
                    log.info(f"'{pdf_file.name}' is exist and replace is False, cancelling download...")
                    return

            # Create volume folder
            volume_path = create_directory(vol_name, self.path)

            for chap_class, chap_images in chapters:

                # Insert "start of the chapter" image
                img_name = count.get() + '.png'
                img_path = volume_path / img_name

                if not self.no_chapter_info:
                    get_chapter_info(chap_class, img_path, self.replace)
                    images.append(img_path)
                    count.increase()

                images.extend(self.get_images(chap_class, chap_images, volume_path, count))

            log.info(f"{vol_name} has finished download, converting to pdf...")

            # Save it as pdf
            worker.submit(lambda: self.convert(images, pdf_file))

            # Remove original chapter folder
            shutil.rmtree(volume_path, ignore_errors=True)

        # Shutdown queue-based thread process
        worker.shutdown()
