"""Real source TEXT splicing and mounted canonical publication, without parser stubs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import deps
import dxf_intake
import project_drawing_editor as editor
import project_drawing_write as writer
import project_string_tag_write as tags
import solar_project_context as context
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest
import test_project_drawing_editor as editor_cases
import test_project_drawing_write as writer_cases
from test_lab_corpus_executor import private_pg  # noqa: F401
from test_project_drawing_editor import checkout, lane, versions  # noqa: F401
from test_w1_design_graph import graph as equipped_graph  # noqa: F401
from test_w2_string_add import solved

TOOL = next(row for row in json.loads((Path(__file__).resolve().parents[1] / "write_tools.json")
            .read_text())["tools"] if row["name"] == "solar-string-tags")
BUILTIN = writer._builtin(TOOL["entry"])


def groups(*rows):
    return "".join(f"{code}\n{value}\n" for code, value in rows).encode("latin-1")


# ezdxf 1.4.3 R2010 source as readable bytes; exact producer hash below.
# No opaque compression, decoding dependency, or security-scan exemption.
_REAL_SOURCE = (
    b'  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1024\n  9\n$ACADMAINTVER\n'
    b' 70\n6\n  9\n$DWGCODEPAGE\n  3\nANSI_1252\n  9\n$LASTSAVEDBY\n  1\nezdxf\n'
    b'  9\n$INSBASE\n 10\n0.0\n 20\n0.0\n 30\n0.0\n  9\n$EXTMIN\n'
    b' 10\n1e+20\n 20\n1e+20\n 30\n1e+20\n  9\n$EXTMAX\n 10\n-1e+20\n'
    b' 20\n-1e+20\n 30\n-1e+20\n  9\n$LIMMIN\n 10\n0.0\n 20\n0.0\n'
    b'  9\n$LIMMAX\n 10\n420.0\n 20\n297.0\n  9\n$ORTHOMODE\n 70\n0\n'
    b'  9\n$REGENMODE\n 70\n1\n  9\n$FILLMODE\n 70\n1\n  9\n$QTEXTMODE\n'
    b' 70\n0\n  9\n$MIRRTEXT\n 70\n1\n  9\n$LTSCALE\n 40\n1.0\n'
    b'  9\n$ATTMODE\n 70\n1\n  9\n$TEXTSIZE\n 40\n2.5\n  9\n$TRACEWID\n'
    b' 40\n1.0\n  9\n$TEXTSTYLE\n  7\nStandard\n  9\n$CLAYER\n  8\n0\n'
    b'  9\n$CELTYPE\n  6\nByLayer\n  9\n$CECOLOR\n 62\n256\n  9\n$CELTSCALE\n'
    b' 40\n1.0\n  9\n$DISPSILH\n 70\n0\n  9\n$DIMSCALE\n 40\n1.0\n'
    b'  9\n$DIMASZ\n 40\n2.5\n  9\n$DIMEXO\n 40\n0.625\n  9\n$DIMDLI\n'
    b' 40\n3.75\n  9\n$DIMRND\n 40\n0.0\n  9\n$DIMDLE\n 40\n0.0\n'
    b'  9\n$DIMEXE\n 40\n1.25\n  9\n$DIMTP\n 40\n0.0\n  9\n$DIMTM\n'
    b' 40\n0.0\n  9\n$DIMTXT\n 40\n2.5\n  9\n$DIMCEN\n 40\n2.5\n'
    b'  9\n$DIMTSZ\n 40\n0.0\n  9\n$DIMTOL\n 70\n0\n  9\n$DIMLIM\n'
    b' 70\n0\n  9\n$DIMTIH\n 70\n0\n  9\n$DIMTOH\n 70\n0\n'
    b'  9\n$DIMSE1\n 70\n0\n  9\n$DIMSE2\n 70\n0\n  9\n$DIMTAD\n'
    b' 70\n1\n  9\n$DIMZIN\n 70\n8\n  9\n$DIMBLK\n  1\n\n'
    b'  9\n$DIMASO\n 70\n1\n  9\n$DIMSHO\n 70\n1\n  9\n$DIMPOST\n'
    b'  1\n\n  9\n$DIMAPOST\n  1\n\n  9\n$DIMALT\n 70\n0\n'
    b'  9\n$DIMALTD\n 70\n3\n  9\n$DIMALTF\n 40\n0.03937007874\n  9\n$DIMLFAC\n'
    b' 40\n1.0\n  9\n$DIMTOFL\n 70\n1\n  9\n$DIMTVP\n 40\n0.0\n'
    b'  9\n$DIMTIX\n 70\n0\n  9\n$DIMSOXD\n 70\n0\n  9\n$DIMSAH\n'
    b' 70\n0\n  9\n$DIMBLK1\n  1\n\n  9\n$DIMBLK2\n  1\n\n'
    b'  9\n$DIMSTYLE\n  2\nISO-25\n  9\n$DIMCLRD\n 70\n0\n  9\n$DIMCLRE\n'
    b' 70\n0\n  9\n$DIMCLRT\n 70\n0\n  9\n$DIMTFAC\n 40\n1.0\n'
    b'  9\n$DIMGAP\n 40\n0.625\n  9\n$DIMJUST\n 70\n0\n  9\n$DIMSD1\n'
    b' 70\n0\n  9\n$DIMSD2\n 70\n0\n  9\n$DIMTOLJ\n 70\n0\n'
    b'  9\n$DIMTZIN\n 70\n8\n  9\n$DIMALTZ\n 70\n0\n  9\n$DIMALTTZ\n'
    b' 70\n0\n  9\n$DIMUPT\n 70\n0\n  9\n$DIMDEC\n 70\n2\n'
    b'  9\n$DIMTDEC\n 70\n2\n  9\n$DIMALTU\n 70\n2\n  9\n$DIMALTTD\n'
    b' 70\n3\n  9\n$DIMTXSTY\n  7\nStandard\n  9\n$DIMAUNIT\n 70\n0\n'
    b'  9\n$DIMADEC\n 70\n0\n  9\n$DIMALTRND\n 40\n0.0\n  9\n$DIMAZIN\n'
    b' 70\n0\n  9\n$DIMDSEP\n 70\n44\n  9\n$DIMATFIT\n 70\n3\n'
    b'  9\n$DIMFRAC\n 70\n0\n  9\n$DIMLDRBLK\n  1\n\n  9\n$DIMLUNIT\n'
    b' 70\n2\n  9\n$DIMLWD\n 70\n-2\n  9\n$DIMLWE\n 70\n-2\n'
    b'  9\n$DIMTMOVE\n 70\n0\n  9\n$DIMFXL\n 40\n1.0\n  9\n$DIMFXLON\n'
    b' 70\n0\n  9\n$DIMJOGANG\n 40\n0.785398163397\n  9\n$DIMTFILL\n 70\n0\n'
    b'  9\n$DIMTFILLCLR\n 70\n0\n  9\n$DIMARCSYM\n 70\n0\n  9\n$DIMLTYPE\n'
    b'  6\n\n  9\n$DIMLTEX1\n  6\n\n  9\n$DIMLTEX2\n  6\n\n'
    b'  9\n$DIMTXTDIRECTION\n 70\n0\n  9\n$LUNITS\n 70\n2\n  9\n$LUPREC\n'
    b' 70\n4\n  9\n$SKETCHINC\n 40\n1.0\n  9\n$FILLETRAD\n 40\n10.0\n'
    b'  9\n$AUNITS\n 70\n0\n  9\n$AUPREC\n 70\n2\n  9\n$MENU\n'
    b'  1\n.\n  9\n$ELEVATION\n 40\n0.0\n  9\n$PELEVATION\n 40\n0.0\n'
    b'  9\n$THICKNESS\n 40\n0.0\n  9\n$LIMCHECK\n 70\n0\n  9\n$CHAMFERA\n'
    b' 40\n0.0\n  9\n$CHAMFERB\n 40\n0.0\n  9\n$CHAMFERC\n 40\n0.0\n'
    b'  9\n$CHAMFERD\n 40\n0.0\n  9\n$SKPOLY\n 70\n0\n  9\n$TDCREATE\n'
    b' 40\n2461361.9604166667\n  9\n$TDUCREATE\n 40\n2458532.153996898\n  9\n$TDUPDATE\n 40\n2461361.9604166667\n'
    b'  9\n$TDUUPDATE\n 40\n2458532.1544311\n  9\n$TDINDWG\n 40\n0.0\n  9\n$TDUSRTIMER\n'
    b' 40\n0.0\n  9\n$USRTIMER\n 70\n1\n  9\n$ANGBASE\n 50\n0.0\n'
    b'  9\n$ANGDIR\n 70\n0\n  9\n$PDMODE\n 70\n0\n  9\n$PDSIZE\n'
    b' 40\n0.0\n  9\n$PLINEWID\n 40\n0.0\n  9\n$SPLFRAME\n 70\n0\n'
    b'  9\n$SPLINETYPE\n 70\n6\n  9\n$SPLINESEGS\n 70\n8\n  9\n$HANDSEED\n'
    b'  5\n35\n  9\n$SURFTAB1\n 70\n6\n  9\n$SURFTAB2\n 70\n6\n'
    b'  9\n$SURFTYPE\n 70\n6\n  9\n$SURFU\n 70\n6\n  9\n$SURFV\n'
    b' 70\n6\n  9\n$UCSBASE\n  2\n\n  9\n$UCSNAME\n  2\n\n'
    b'  9\n$UCSORG\n 10\n0.0\n 20\n0.0\n 30\n0.0\n  9\n$UCSXDIR\n'
    b' 10\n1.0\n 20\n0.0\n 30\n0.0\n  9\n$UCSYDIR\n 10\n0.0\n'
    b' 20\n1.0\n 30\n0.0\n  9\n$UCSORTHOREF\n  2\n\n  9\n$UCSORTHOVIEW\n'
    b' 70\n0\n  9\n$UCSORGTOP\n 10\n0.0\n 20\n0.0\n 30\n0.0\n'
    b'  9\n$UCSORGBOTTOM\n 10\n0.0\n 20\n0.0\n 30\n0.0\n  9\n$UCSORGLEFT\n'
    b' 10\n0.0\n 20\n0.0\n 30\n0.0\n  9\n$UCSORGRIGHT\n 10\n0.0\n'
    b' 20\n0.0\n 30\n0.0\n  9\n$UCSORGFRONT\n 10\n0.0\n 20\n0.0\n'
    b' 30\n0.0\n  9\n$UCSORGBACK\n 10\n0.0\n 20\n0.0\n 30\n0.0\n'
    b'  9\n$PUCSBASE\n  2\n\n  9\n$PUCSNAME\n  2\n\n  9\n$PUCSORG\n'
    b' 10\n0.0\n 20\n0.0\n 30\n0.0\n  9\n$PUCSXDIR\n 10\n1.0\n'
    b' 20\n0.0\n 30\n0.0\n  9\n$PUCSYDIR\n 10\n0.0\n 20\n1.0\n'
    b' 30\n0.0\n  9\n$PUCSORTHOREF\n  2\n\n  9\n$PUCSORTHOVIEW\n 70\n0\n'
    b'  9\n$PUCSORGTOP\n 10\n0.0\n 20\n0.0\n 30\n0.0\n  9\n$PUCSORGBOTTOM\n'
    b' 10\n0.0\n 20\n0.0\n 30\n0.0\n  9\n$PUCSORGLEFT\n 10\n0.0\n'
    b' 20\n0.0\n 30\n0.0\n  9\n$PUCSORGRIGHT\n 10\n0.0\n 20\n0.0\n'
    b' 30\n0.0\n  9\n$PUCSORGFRONT\n 10\n0.0\n 20\n0.0\n 30\n0.0\n'
    b'  9\n$PUCSORGBACK\n 10\n0.0\n 20\n0.0\n 30\n0.0\n  9\n$USERI1\n'
    b' 70\n0\n  9\n$USERI2\n 70\n0\n  9\n$USERI3\n 70\n0\n'
    b'  9\n$USERI4\n 70\n0\n  9\n$USERI5\n 70\n0\n  9\n$USERR1\n'
    b' 40\n0.0\n  9\n$USERR2\n 40\n0.0\n  9\n$USERR3\n 40\n0.0\n'
    b'  9\n$USERR4\n 40\n0.0\n  9\n$USERR5\n 40\n0.0\n  9\n$WORLDVIEW\n'
    b' 70\n1\n  9\n$SHADEDGE\n 70\n3\n  9\n$SHADEDIF\n 70\n70\n'
    b'  9\n$TILEMODE\n 70\n1\n  9\n$MAXACTVP\n 70\n64\n  9\n$PINSBASE\n'
    b' 10\n0.0\n 20\n0.0\n 30\n0.0\n  9\n$PLIMCHECK\n 70\n0\n'
    b'  9\n$PEXTMIN\n 10\n1e+20\n 20\n1e+20\n 30\n1e+20\n  9\n$PEXTMAX\n'
    b' 10\n-1e+20\n 20\n-1e+20\n 30\n-1e+20\n  9\n$PLIMMIN\n 10\n0.0\n'
    b' 20\n0.0\n  9\n$PLIMMAX\n 10\n420.0\n 20\n297.0\n  9\n$UNITMODE\n'
    b' 70\n0\n  9\n$VISRETAIN\n 70\n1\n  9\n$PLINEGEN\n 70\n0\n'
    b'  9\n$PSLTSCALE\n 70\n1\n  9\n$TREEDEPTH\n 70\n3020\n  9\n$CMLSTYLE\n'
    b'  2\nStandard\n  9\n$CMLJUST\n 70\n0\n  9\n$CMLSCALE\n 40\n20.0\n'
    b'  9\n$PROXYGRAPHICS\n 70\n1\n  9\n$MEASUREMENT\n 70\n1\n  9\n$CELWEIGHT\n'
    b'370\n-1\n  9\n$ENDCAPS\n280\n0\n  9\n$JOINSTYLE\n280\n0\n'
    b'  9\n$LWDISPLAY\n290\n0\n  9\n$INSUNITS\n 70\n1\n  9\n$HYPERLINKBASE\n'
    b'  1\n\n  9\n$STYLESHEET\n  1\n\n  9\n$XEDIT\n290\n1\n'
    b'  9\n$CEPSNTYPE\n380\n0\n  9\n$PSTYLEMODE\n290\n1\n  9\n$FINGERPRINTGUID\n'
    b'  2\n{67CB5B24-502D-4E1E-A004-34D9DC0309A6}\n  9\n$VERSIONGUID\n  2\n{19B8A595-A104-46D6-A3BD-CCD5D40049D5}\n  9\n$EXTNAMES\n290\n1\n'
    b'  9\n$PSVPSCALE\n 40\n0.0\n  9\n$OLESTARTUP\n290\n0\n  9\n$SORTENTS\n'
    b'280\n127\n  9\n$INDEXCTL\n280\n0\n  9\n$HIDETEXT\n280\n1\n'
    b'  9\n$XCLIPFRAME\n280\n1\n  9\n$HALOGAP\n280\n0\n  9\n$OBSCOLOR\n'
    b' 70\n257\n  9\n$OBSLTYPE\n280\n0\n  9\n$INTERSECTIONDISPLAY\n280\n0\n'
    b'  9\n$INTERSECTIONCOLOR\n 70\n257\n  9\n$DIMASSOC\n280\n2\n  9\n$PROJECTNAME\n'
    b'  1\n\n  9\n$CAMERADISPLAY\n290\n0\n  9\n$LENSLENGTH\n 40\n50.0\n'
    b'  9\n$CAMERAHEIGHT\n 40\n0.0\n  9\n$STEPSPERSEC\n 40\n24.0\n  9\n$STEPSIZE\n'
    b' 40\n100.0\n  9\n$3DDWFPREC\n 40\n2.0\n  9\n$PSOLWIDTH\n 40\n0.005\n'
    b'  9\n$PSOLHEIGHT\n 40\n0.08\n  9\n$LOFTANG1\n 40\n1.570796326795\n  9\n$LOFTANG2\n'
    b' 40\n1.570796326795\n  9\n$LOFTMAG1\n 40\n0.0\n  9\n$LOFTMAG2\n 40\n0.0\n'
    b'  9\n$LOFTPARAM\n 70\n7\n  9\n$LOFTNORMALS\n280\n1\n  9\n$LATITUDE\n'
    b' 40\n37.795\n  9\n$LONGITUDE\n 40\n-122.394\n  9\n$NORTHDIRECTION\n 40\n0.0\n'
    b'  9\n$TIMEZONE\n 70\n-8000\n  9\n$LIGHTGLYPHDISPLAY\n280\n1\n  9\n$TILEMODELIGHTSYNCH\n'
    b'280\n1\n  9\n$CMATERIAL\n347\n20\n  9\n$SOLIDHIST\n280\n0\n'
    b'  9\n$SHOWHIST\n280\n1\n  9\n$DWFFRAME\n280\n2\n  9\n$DGNFRAME\n'
    b'280\n2\n  9\n$REALWORLDSCALE\n290\n1\n  9\n$INTERFERECOLOR\n 62\n256\n'
    b'  9\n$CSHADOW\n280\n0\n  9\n$SHADOWPLANELOCATION\n 40\n0.0\n  0\nENDSEC\n'
    b'  0\nSECTION\n  2\nCLASSES\n  0\nCLASS\n  1\nACDBDICTIONARYWDFLT\n  2\nAcDbDictionaryWithDefault\n'
    b'  3\nObjectDBX Classes\n 90\n0\n 91\n0\n280\n0\n281\n0\n'
    b'  0\nCLASS\n  1\nSUN\n  2\nAcDbSun\n  3\nSCENEOE\n 90\n1153\n'
    b' 91\n0\n280\n0\n281\n0\n  0\nCLASS\n  1\nVISUALSTYLE\n'
    b'  2\nAcDbVisualStyle\n  3\nObjectDBX Classes\n 90\n4095\n 91\n0\n280\n0\n'
    b'281\n0\n  0\nCLASS\n  1\nMATERIAL\n  2\nAcDbMaterial\n  3\nObjectDBX Classes\n'
    b' 90\n1153\n 91\n0\n280\n0\n281\n0\n  0\nCLASS\n'
    b'  1\nSCALE\n  2\nAcDbScale\n  3\nObjectDBX Classes\n 90\n1153\n 91\n0\n'
    b'280\n0\n281\n0\n  0\nCLASS\n  1\nTABLESTYLE\n  2\nAcDbTableStyle\n'
    b'  3\nObjectDBX Classes\n 90\n4095\n 91\n0\n280\n0\n281\n0\n'
    b'  0\nCLASS\n  1\nMLEADERSTYLE\n  2\nAcDbMLeaderStyle\n  3\nACDB_MLEADERSTYLE_CLASS\n 90\n4095\n'
    b' 91\n0\n280\n0\n281\n0\n  0\nCLASS\n  1\nDICTIONARYVAR\n'
    b'  2\nAcDbDictionaryVar\n  3\nObjectDBX Classes\n 90\n0\n 91\n0\n280\n0\n'
    b'281\n0\n  0\nCLASS\n  1\nCELLSTYLEMAP\n  2\nAcDbCellStyleMap\n  3\nObjectDBX Classes\n'
    b' 90\n1152\n 91\n0\n280\n0\n281\n0\n  0\nCLASS\n'
    b'  1\nMENTALRAYRENDERSETTINGS\n  2\nAcDbMentalRayRenderSettings\n  3\nSCENEOE\n 90\n1024\n 91\n0\n'
    b'280\n0\n281\n0\n  0\nCLASS\n  1\nACDBDETAILVIEWSTYLE\n  2\nAcDbDetailViewStyle\n'
    b'  3\nObjectDBX Classes\n 90\n1025\n 91\n0\n280\n0\n281\n0\n'
    b'  0\nCLASS\n  1\nACDBSECTIONVIEWSTYLE\n  2\nAcDbSectionViewStyle\n  3\nObjectDBX Classes\n 90\n1025\n'
    b' 91\n0\n280\n0\n281\n0\n  0\nCLASS\n  1\nRASTERVARIABLES\n'
    b'  2\nAcDbRasterVariables\n  3\nISM\n 90\n0\n 91\n0\n280\n0\n'
    b'281\n0\n  0\nCLASS\n  1\nACDBPLACEHOLDER\n  2\nAcDbPlaceHolder\n  3\nObjectDBX Classes\n'
    b' 90\n0\n 91\n0\n280\n0\n281\n0\n  0\nCLASS\n'
    b'  1\nLAYOUT\n  2\nAcDbLayout\n  3\nObjectDBX Classes\n 90\n0\n 91\n0\n'
    b'280\n0\n281\n0\n  0\nENDSEC\n  0\nSECTION\n  2\nTABLES\n'
    b'  0\nTABLE\n  2\nVPORT\n  5\n8\n330\n0\n100\nAcDbSymbolTable\n'
    b' 70\n1\n  0\nVPORT\n  5\n23\n330\n8\n100\nAcDbSymbolTableRecord\n'
    b'100\nAcDbViewportTableRecord\n  2\n*Active\n 70\n0\n 10\n0.0\n 20\n0.0\n'
    b' 11\n1.0\n 21\n1.0\n 12\n0.0\n 22\n0.0\n 13\n0.0\n'
    b' 23\n0.0\n 14\n0.5\n 24\n0.5\n 15\n0.5\n 25\n0.5\n'
    b' 16\n0.0\n 26\n0.0\n 36\n1.0\n 17\n0.0\n 27\n0.0\n'
    b' 37\n0.0\n 40\n1000.0\n 41\n1.34\n 42\n50.0\n 43\n0.0\n'
    b' 44\n0.0\n 50\n0.0\n 51\n0.0\n 71\n0\n 72\n1000\n'
    b' 73\n1\n 74\n3\n 75\n0\n 76\n0\n 77\n0\n'
    b' 78\n0\n281\n0\n 65\n0\n146\n0.0\n  0\nENDTAB\n'
    b'  0\nTABLE\n  2\nLTYPE\n  5\n2\n330\n0\n100\nAcDbSymbolTable\n'
    b' 70\n3\n  0\nLTYPE\n  5\n24\n330\n2\n100\nAcDbSymbolTableRecord\n'
    b'100\nAcDbLinetypeTableRecord\n  2\nByBlock\n 70\n0\n  3\n\n 72\n65\n'
    b' 73\n0\n 40\n0.0\n  0\nLTYPE\n  5\n25\n330\n2\n'
    b'100\nAcDbSymbolTableRecord\n100\nAcDbLinetypeTableRecord\n  2\nByLayer\n 70\n0\n  3\n\n'
    b' 72\n65\n 73\n0\n 40\n0.0\n  0\nLTYPE\n  5\n26\n'
    b'330\n2\n100\nAcDbSymbolTableRecord\n100\nAcDbLinetypeTableRecord\n  2\nContinuous\n 70\n0\n'
    b'  3\n\n 72\n65\n 73\n0\n 40\n0.0\n  0\nENDTAB\n'
    b'  0\nTABLE\n  2\nLAYER\n  5\n1\n330\n0\n100\nAcDbSymbolTable\n'
    b' 70\n3\n  0\nLAYER\n  5\n27\n330\n1\n100\nAcDbSymbolTableRecord\n'
    b'100\nAcDbLayerTableRecord\n  2\n0\n 70\n0\n 62\n7\n  6\nContinuous\n'
    b'370\n-3\n390\n13\n347\n21\n  0\nLAYER\n  5\n28\n'
    b'330\n1\n100\nAcDbSymbolTableRecord\n100\nAcDbLayerTableRecord\n  2\nDefpoints\n 70\n0\n'
    b' 62\n7\n  6\nContinuous\n290\n0\n370\n-3\n390\n13\n'
    b'347\n21\n  0\nLAYER\n  5\n2F\n330\n1\n100\nAcDbSymbolTableRecord\n'
    b'100\nAcDbLayerTableRecord\n  2\nNotes\n 70\n0\n 62\n3\n  6\nContinuous\n'
    b'370\n-3\n390\n13\n347\n21\n  0\nENDTAB\n  0\nTABLE\n'
    b'  2\nSTYLE\n  5\n5\n330\n0\n100\nAcDbSymbolTable\n 70\n1\n'
    b'  0\nSTYLE\n  5\n29\n330\n5\n100\nAcDbSymbolTableRecord\n100\nAcDbTextStyleTableRecord\n'
    b'  2\nStandard\n 70\n0\n 40\n0.0\n 41\n1.0\n 50\n0.0\n'
    b' 71\n0\n 42\n2.5\n  3\ntxt\n  4\n\n  0\nENDTAB\n'
    b'  0\nTABLE\n  2\nVIEW\n  5\n7\n330\n0\n100\nAcDbSymbolTable\n'
    b' 70\n0\n  0\nENDTAB\n  0\nTABLE\n  2\nUCS\n  5\n6\n'
    b'330\n0\n100\nAcDbSymbolTable\n 70\n0\n  0\nENDTAB\n  0\nTABLE\n'
    b'  2\nAPPID\n  5\n3\n330\n0\n100\nAcDbSymbolTable\n 70\n3\n'
    b'  0\nAPPID\n  5\n2A\n330\n3\n100\nAcDbSymbolTableRecord\n100\nAcDbRegAppTableRecord\n'
    b'  2\nACAD\n 70\n0\n  0\nAPPID\n  5\n32\n330\n3\n'
    b'100\nAcDbSymbolTableRecord\n100\nAcDbRegAppTableRecord\n  2\nHATCHBACKGROUNDCOLOR\n 70\n0\n  0\nAPPID\n'
    b'  5\n33\n330\n3\n100\nAcDbSymbolTableRecord\n100\nAcDbRegAppTableRecord\n  2\nEZDXF\n'
    b' 70\n0\n  0\nENDTAB\n  0\nTABLE\n  2\nDIMSTYLE\n  5\n4\n'
    b'330\n0\n100\nAcDbSymbolTable\n 70\n1\n100\nAcDbDimStyleTable\n  0\nDIMSTYLE\n'
    b'105\n2B\n330\n4\n100\nAcDbSymbolTableRecord\n100\nAcDbDimStyleTableRecord\n  2\nStandard\n'
    b' 70\n0\n 40\n1.0\n 41\n2.5\n 42\n0.625\n 43\n3.75\n'
    b' 44\n1.25\n 45\n0.0\n 46\n0.0\n 47\n0.0\n 48\n0.0\n'
    b' 49\n2.5\n140\n2.5\n141\n2.5\n142\n0.0\n143\n0.03937007874\n'
    b'144\n1.0\n145\n0.0\n146\n1.0\n147\n0.625\n148\n0.0\n'
    b' 69\n0\n 70\n0\n 71\n0\n 72\n0\n 73\n0\n'
    b' 74\n0\n 75\n0\n 76\n0\n 77\n1\n 78\n8\n'
    b' 79\n3\n170\n0\n171\n3\n172\n1\n173\n0\n'
    b'174\n0\n175\n0\n176\n0\n177\n0\n178\n0\n'
    b'179\n2\n271\n2\n272\n2\n273\n2\n274\n3\n'
    b'275\n0\n276\n0\n277\n2\n278\n44\n279\n0\n'
    b'280\n0\n281\n0\n282\n0\n283\n0\n284\n8\n'
    b'285\n0\n286\n0\n288\n0\n289\n3\n290\n0\n'
    b'371\n-2\n372\n-2\n  0\nENDTAB\n  0\nTABLE\n  2\nBLOCK_RECORD\n'
    b'  5\n9\n330\n0\n100\nAcDbSymbolTable\n 70\n2\n  0\nBLOCK_RECORD\n'
    b'  5\n17\n330\n9\n100\nAcDbSymbolTableRecord\n100\nAcDbBlockTableRecord\n  2\n*Model_Space\n'
    b'340\n1A\n 70\n0\n280\n1\n281\n0\n  0\nBLOCK_RECORD\n'
    b'  5\n1B\n330\n9\n100\nAcDbSymbolTableRecord\n100\nAcDbBlockTableRecord\n  2\n*Paper_Space\n'
    b'340\n1E\n 70\n0\n280\n1\n281\n0\n  0\nENDTAB\n'
    b'  0\nENDSEC\n  0\nSECTION\n  2\nBLOCKS\n  0\nBLOCK\n  5\n18\n'
    b'330\n17\n100\nAcDbEntity\n  8\n0\n100\nAcDbBlockBegin\n  2\n*Model_Space\n'
    b' 70\n0\n 10\n0.0\n 20\n0.0\n 30\n0.0\n  3\n*Model_Space\n'
    b'  1\n\n  0\nENDBLK\n  5\n19\n330\n17\n100\nAcDbEntity\n'
    b'  8\n0\n100\nAcDbBlockEnd\n  0\nBLOCK\n  5\n1C\n330\n1B\n'
    b'100\nAcDbEntity\n  8\n0\n100\nAcDbBlockBegin\n  2\n*Paper_Space\n 70\n0\n'
    b' 10\n0.0\n 20\n0.0\n 30\n0.0\n  3\n*Paper_Space\n  1\n\n'
    b'  0\nENDBLK\n  5\n1D\n330\n1B\n100\nAcDbEntity\n  8\n0\n'
    b'100\nAcDbBlockEnd\n  0\nENDSEC\n  0\nSECTION\n  2\nENTITIES\n  0\nTEXT\n'
    b'  5\nA1\n330\n17\n100\nAcDbEntity\n  8\nNotes\n100\nAcDbText\n'
    b' 10\n15.0\n 20\n25.0\n 30\n0.0\n 40\n5.0\n  1\nCustomer label\n'
    b'100\nAcDbText\n  0\nLINE\n  5\nA2\n330\n17\n100\nAcDbEntity\n'
    b'  8\nNotes\n100\nAcDbLine\n 10\n0.0\n 20\n0.0\n 30\n0.0\n'
    b' 11\n40.0\n 21\n80.0\n 31\n0.0\n  0\nENDSEC\n  0\nSECTION\n'
    b'  2\nOBJECTS\n  0\nDICTIONARY\n  5\nA\n330\n0\n100\nAcDbDictionary\n'
    b'281\n1\n  3\nACAD_COLOR\n350\nB\n  3\nACAD_GROUP\n350\nC\n'
    b'  3\nACAD_LAYOUT\n350\nD\n  3\nACAD_MATERIAL\n350\nE\n  3\nACAD_MLEADERSTYLE\n'
    b'350\nF\n  3\nACAD_MLINESTYLE\n350\n10\n  3\nACAD_PLOTSETTINGS\n350\n11\n'
    b'  3\nACAD_PLOTSTYLENAME\n350\n12\n  3\nACAD_SCALELIST\n350\n14\n  3\nACAD_TABLESTYLE\n'
    b'350\n15\n  3\nACAD_VISUALSTYLE\n350\n16\n  3\nEZDXF_META\n350\n2D\n'
    b'  0\nDICTIONARY\n  5\nB\n330\nA\n100\nAcDbDictionary\n281\n1\n'
    b'  0\nDICTIONARY\n  5\nC\n330\nA\n100\nAcDbDictionary\n281\n1\n'
    b'  0\nDICTIONARY\n  5\nD\n330\nA\n100\nAcDbDictionary\n281\n1\n'
    b'  3\nModel\n350\n1A\n  3\nLayout1\n350\n1E\n  0\nDICTIONARY\n'
    b'  5\nE\n330\nA\n100\nAcDbDictionary\n281\n1\n  3\nByBlock\n'
    b'350\n1F\n  3\nByLayer\n350\n20\n  3\nGlobal\n350\n21\n'
    b'  0\nDICTIONARY\n  5\nF\n330\nA\n100\nAcDbDictionary\n281\n1\n'
    b'  3\nStandard\n350\n2C\n  0\nDICTIONARY\n  5\n10\n330\nA\n'
    b'100\nAcDbDictionary\n281\n1\n  3\nStandard\n350\n22\n  0\nDICTIONARY\n'
    b'  5\n11\n330\nA\n100\nAcDbDictionary\n281\n1\n  0\nACDBDICTIONARYWDFLT\n'
    b'  5\n12\n330\nA\n100\nAcDbDictionary\n281\n1\n  3\nNormal\n'
    b'350\n13\n100\nAcDbDictionaryWithDefault\n340\n13\n  0\nACDBPLACEHOLDER\n  5\n13\n'
    b'330\n12\n  0\nDICTIONARY\n  5\n14\n330\nA\n100\nAcDbDictionary\n'
    b'281\n1\n  0\nDICTIONARY\n  5\n15\n330\nA\n100\nAcDbDictionary\n'
    b'281\n1\n  0\nDICTIONARY\n  5\n16\n330\nA\n100\nAcDbDictionary\n'
    b'281\n1\n  0\nLAYOUT\n  5\n1A\n330\nD\n100\nAcDbPlotSettings\n'
    b'  1\n\n  4\nA3\n  6\n\n 40\n7.5\n 41\n20.0\n'
    b' 42\n7.5\n 43\n20.0\n 44\n420.0\n 45\n297.0\n 46\n0.0\n'
    b' 47\n0.0\n 48\n0.0\n 49\n0.0\n140\n0.0\n141\n0.0\n'
    b'142\n1.0\n143\n1.0\n 70\n1024\n 72\n1\n 73\n0\n'
    b' 74\n5\n  7\n\n 75\n16\n 76\n0\n 77\n2\n'
    b' 78\n300\n147\n1.0\n148\n0.0\n149\n0.0\n100\nAcDbLayout\n'
    b'  1\nModel\n 70\n1\n 71\n0\n 10\n0.0\n 20\n0.0\n'
    b' 11\n420.0\n 21\n297.0\n 12\n0.0\n 22\n0.0\n 32\n0.0\n'
    b' 14\n1e+20\n 24\n1e+20\n 34\n1e+20\n 15\n-1e+20\n 25\n-1e+20\n'
    b' 35\n-1e+20\n146\n0.0\n 13\n0.0\n 23\n0.0\n 33\n0.0\n'
    b' 16\n1.0\n 26\n0.0\n 36\n0.0\n 17\n0.0\n 27\n1.0\n'
    b' 37\n0.0\n 76\n1\n330\n17\n  0\nLAYOUT\n  5\n1E\n'
    b'330\nD\n100\nAcDbPlotSettings\n  1\n\n  4\nA3\n  6\n\n'
    b' 40\n7.5\n 41\n20.0\n 42\n7.5\n 43\n20.0\n 44\n420.0\n'
    b' 45\n297.0\n 46\n0.0\n 47\n0.0\n 48\n0.0\n 49\n0.0\n'
    b'140\n0.0\n141\n0.0\n142\n1.0\n143\n1.0\n 70\n0\n'
    b' 72\n1\n 73\n0\n 74\n5\n  7\n\n 75\n16\n'
    b' 76\n0\n 77\n2\n 78\n300\n147\n1.0\n148\n0.0\n'
    b'149\n0.0\n100\nAcDbLayout\n  1\nLayout1\n 70\n1\n 71\n1\n'
    b' 10\n0.0\n 20\n0.0\n 11\n420.0\n 21\n297.0\n 12\n0.0\n'
    b' 22\n0.0\n 32\n0.0\n 14\n1e+20\n 24\n1e+20\n 34\n1e+20\n'
    b' 15\n-1e+20\n 25\n-1e+20\n 35\n-1e+20\n146\n0.0\n 13\n0.0\n'
    b' 23\n0.0\n 33\n0.0\n 16\n1.0\n 26\n0.0\n 36\n0.0\n'
    b' 17\n0.0\n 27\n1.0\n 37\n0.0\n 76\n1\n330\n1B\n'
    b'  0\nMATERIAL\n  5\n1F\n102\n{ACAD_REACTORS\n330\nE\n102\n}\n'
    b'330\nE\n100\nAcDbMaterial\n  1\nByBlock\n  2\n\n 70\n0\n'
    b' 40\n1.0\n 71\n1\n 41\n1.0\n 91\n-1023410177\n 42\n1.0\n'
    b' 72\n1\n  3\n\n 73\n1\n 74\n1\n 75\n1\n'
    b' 44\n0.5\n 73\n0\n 45\n1.0\n 46\n1.0\n 77\n1\n'
    b'  4\n\n 78\n1\n 79\n1\n170\n1\n 48\n1.0\n'
    b'171\n1\n  6\n\n172\n1\n173\n1\n174\n1\n'
    b'140\n1.0\n141\n1.0\n175\n1\n  7\n\n176\n1\n'
    b'177\n1\n178\n1\n143\n1.0\n179\n1\n  8\n\n'
    b'270\n1\n271\n1\n272\n1\n145\n1.0\n146\n1.0\n'
    b'273\n1\n  9\n\n274\n1\n275\n1\n276\n1\n'
    b' 42\n1.0\n 72\n1\n  3\n\n 73\n1\n 74\n1\n'
    b' 75\n1\n 94\n63\n  0\nMATERIAL\n  5\n20\n102\n{ACAD_REACTORS\n'
    b'330\nE\n102\n}\n330\nE\n100\nAcDbMaterial\n  1\nByLayer\n'
    b'  2\n\n 70\n0\n 40\n1.0\n 71\n1\n 41\n1.0\n'
    b' 91\n-1023410177\n 42\n1.0\n 72\n1\n  3\n\n 73\n1\n'
    b' 74\n1\n 75\n1\n 44\n0.5\n 73\n0\n 45\n1.0\n'
    b' 46\n1.0\n 77\n1\n  4\n\n 78\n1\n 79\n1\n'
    b'170\n1\n 48\n1.0\n171\n1\n  6\n\n172\n1\n'
    b'173\n1\n174\n1\n140\n1.0\n141\n1.0\n175\n1\n'
    b'  7\n\n176\n1\n177\n1\n178\n1\n143\n1.0\n'
    b'179\n1\n  8\n\n270\n1\n271\n1\n272\n1\n'
    b'145\n1.0\n146\n1.0\n273\n1\n  9\n\n274\n1\n'
    b'275\n1\n276\n1\n 42\n1.0\n 72\n1\n  3\n\n'
    b' 73\n1\n 74\n1\n 75\n1\n 94\n63\n  0\nMATERIAL\n'
    b'  5\n21\n102\n{ACAD_REACTORS\n330\nE\n102\n}\n330\nE\n'
    b'100\nAcDbMaterial\n  1\nGlobal\n  2\n\n 70\n0\n 40\n1.0\n'
    b' 71\n1\n 41\n1.0\n 91\n-1023410177\n 42\n1.0\n 72\n1\n'
    b'  3\n\n 73\n1\n 74\n1\n 75\n1\n 44\n0.5\n'
    b' 73\n0\n 45\n1.0\n 46\n1.0\n 77\n1\n  4\n\n'
    b' 78\n1\n 79\n1\n170\n1\n 48\n1.0\n171\n1\n'
    b'  6\n\n172\n1\n173\n1\n174\n1\n140\n1.0\n'
    b'141\n1.0\n175\n1\n  7\n\n176\n1\n177\n1\n'
    b'178\n1\n143\n1.0\n179\n1\n  8\n\n270\n1\n'
    b'271\n1\n272\n1\n145\n1.0\n146\n1.0\n273\n1\n'
    b'  9\n\n274\n1\n275\n1\n276\n1\n 42\n1.0\n'
    b' 72\n1\n  3\n\n 73\n1\n 74\n1\n 75\n1\n'
    b' 94\n63\n  0\nMLINESTYLE\n  5\n22\n102\n{ACAD_REACTORS\n330\n10\n'
    b'102\n}\n330\n10\n100\nAcDbMlineStyle\n  2\nStandard\n 70\n0\n'
    b'  3\n\n 62\n256\n 51\n90.0\n 52\n90.0\n 71\n2\n'
    b' 49\n0.5\n 62\n256\n  6\nBYLAYER\n 49\n-0.5\n 62\n256\n'
    b'  6\nBYLAYER\n  0\nMLEADERSTYLE\n  5\n2C\n102\n{ACAD_REACTORS\n330\nF\n'
    b'102\n}\n330\nF\n100\nAcDbMLeaderStyle\n179\n2\n170\n2\n'
    b'171\n1\n172\n0\n 90\n2\n 40\n0.0\n 41\n0.0\n'
    b'173\n1\n 91\n-1056964608\n 92\n-2\n290\n1\n 42\n2.0\n'
    b'291\n1\n 43\n8.0\n  3\nStandard\n 44\n4.0\n300\n\n'
    b'342\n29\n174\n1\n175\n1\n176\n0\n178\n1\n'
    b' 93\n-1056964608\n 45\n4.0\n292\n0\n297\n0\n 46\n4.0\n'
    b' 94\n-1056964608\n 47\n1.0\n 49\n1.0\n140\n1.0\n294\n1\n'
    b'141\n0.0\n177\n0\n142\n1.0\n295\n0\n296\n0\n'
    b'143\n3.75\n271\n0\n272\n9\n273\n9\n  0\nDICTIONARY\n'
    b'  5\n2D\n330\nA\n100\nAcDbDictionary\n280\n1\n281\n1\n'
    b'  3\nCREATED_BY_EZDXF\n350\n2E\n  3\nWRITTEN_BY_EZDXF\n350\n34\n  0\nDICTIONARYVAR\n'
    b'  5\n2E\n330\n2D\n100\nDictionaryVariables\n280\n0\n  1\n1.4.3 @ 2026-11-17T05:03:00.108882+00:00\n'
    b'  0\nDICTIONARYVAR\n  5\n34\n330\n2D\n100\nDictionaryVariables\n280\n0\n'
    b'  1\n1.4.3 @ 2026-11-17T05:03:00.109388+00:00\n  0\nENDSEC\n999\ncaf\xe9 raw source\n  0\nEOF\n'
)
assert len(_REAL_SOURCE) == 15657
assert hashlib.sha256(_REAL_SOURCE).hexdigest() == "bf31b9596eaa6bc9967f87d82197031de51294d873ece34bf6ff9a7eac105ab2"


def source(unit=1):
    """Change only HEADER INSUNITS in the genuine producer source."""
    pairs, _ = writer._pairs(_REAL_SOURCE)
    index = next(i + 1 for i, p in enumerate(pairs) if p.code == 9 and p.value == "$INSUNITS")
    original = pairs[index].raw
    code_line = original.splitlines(keepends=True)[0]
    return b"".join(code_line + str(unit).encode("ascii") + b"\n" if i == index else p.raw
                    for i, p in enumerate(pairs))


def companion(raw, graph):
    intake = dxf_intake.parse_dxf_bytes(raw, source_name="source")
    intake.update(solar_design_graph=deepcopy(graph), solar_design_graph_sha256=digest(graph),
                  project_material={"retained": ["exact", 42]})
    return intake


def prepared(graph, **params):
    return {"expected_rev": graph["rev"], "paper_mm_per_drawing_unit": .254, **params}


def records(raw, section="ENTITIES"):
    pairs, _ = writer._pairs(raw)
    return [pairs[a:b] for a, b in writer._records(pairs, writer._section(pairs, section))]


def owned(raw):
    return [r for r in records(raw) if any(p.code == 1001 and p.value == tags.APP for p in r)]


def value(record, code):
    return next(p.value for p in record if p.code == code)


NOTE = next(b"".join(p.raw for p in r) for r in records(_REAL_SOURCE) if value(r, 5) == "A1")
LINE = next(b"".join(p.raw for p in r) for r in records(_REAL_SOURCE) if value(r, 5) == "A2")
LAYER = next(b"".join(p.raw for p in r) for r in records(_REAL_SOURCE, "TABLES")
             if r[0].value == "LAYER" and value(r, 2) == "Notes")


@pytest.mark.parametrize("unit,scale", [(1, .0254), (6, 1), (0, 1)])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_real_builtin_splice_and_upsert(solved, unit, scale, newline):
    graph = deepcopy(solved)
    graph["project"]["units"].update(drawing_units="in" if unit == 1 else "m",
        meters_per_unit=scale, source="explicit", drawing_unit_is_feet=False)
    raw = source(unit).replace(b"\n", newline)
    intake = companion(raw, graph)
    original, original_intake = bytes(raw), deepcopy(intake)
    params = prepared(graph, paper_mm_per_drawing_unit=10 * scale)
    result, overlay = BUILTIN.run(intake, params)
    assert overlay is None and result["tag_count"] == len(graph["strings"])
    plan = result["string_tag_plan"]
    output, metadata = tags.splice_tags(raw, intake, plan)
    assert metadata["label_moves"] == []
    assert raw == original and intake == original_intake
    assert tags.splice_tags(raw, intake, plan) == (output, metadata)
    source_pairs, _ = writer._pairs(source(unit))
    unit_index = next(i for i, p in enumerate(source_pairs) if p.code == 9 and p.value == "$INSUNITS")
    unit_groups = source_pairs[unit_index].raw + source_pairs[unit_index + 1].raw
    comments = b"".join(p.raw for p in source_pairs if p.code == 999)
    assert comments and b"\xe9" in comments
    for kept in (NOTE, LINE, LAYER, unit_groups, comments):
        assert kept.replace(b"\n", newline) in output
    for section in ("TABLES", "BLOCKS", "ENTITIES", "OBJECTS"):
        for rec in records(raw, section):
            if rec[0].value == "TABLE" and value(rec, 2) in ("LAYER", "STYLE", "APPID"):
                continue  # Only these existing table counts change.
            assert b"".join(p.raw for p in rec) in output
    assert b"\r\n" in output if newline == b"\r\n" else b"\r" not in output
    text_records = owned(output)
    assert len(text_records) == len(graph["strings"])
    assert len({value(r, 5) for r in text_records}) == len(text_records)
    boxes = []
    labels = {s["id"]: s["circuit_tag"] for s in graph["strings"]}
    for rec in text_records:
        data = [p.value for p in rec if p.code == 1000]
        assert data[0] == tags.SCHEMA and value(rec, 1) == labels[data[1]]
        assert value(rec, 330) == "17" and value(rec, 7) == tags.STYLE
        assert value(rec, 72) == value(rec, 73) == value(rec, 50) == "0"
        h, x, y = (float(value(rec, c)) for c in (40, 10, 20))
        assert h * params["paper_mm_per_drawing_unit"] == pytest.approx(2)
        boxes.append((x, y, x + 1.2 * len(value(rec, 1)) * h, y + h))
    for i, (ax, ay, bx, by) in enumerate(boxes):
        for cx, cy, dx, dy in boxes[i + 1:]:
            assert bx < cx or dx < ax or by < cy or dy < ay
    table_rows = records(output, "TABLES")
    layer = next(r for r in table_rows if r[0].value == "LAYER" and value(r, 2) == plan["tag_layer"])
    assert value(layer, 70) == "0" and value(layer, 290) == "1" and int(value(layer, 62)) > 0
    zero_layer = next(r for r in table_rows if r[0].value == "LAYER" and value(r, 2) == "0")
    assert value(layer, 370) == "-3"
    assert value(layer, 390) == value(zero_layer, 390) == "13"
    assert value(layer, 390) in {value(r, 5) for r in records(output, "OBJECTS")}
    style = next(r for r in table_rows if r[0].value == "STYLE" and value(r, 2) == tags.STYLE)
    assert value(style, 3) == "txt" and value(style, 40) == value(style, 50) == "0"
    assert value(style, 41) == "1"
    merged = json.loads(tags.annotation_intake(output, intake, metadata, uuid4()))
    assert merged["solar_design_graph"] == graph
    assert merged["solar_design_graph_sha256"] == digest(graph)
    assert merged["project_material"] == intake["project_material"]
    child, child_metadata = tags.splice_tags(output, merged, BUILTIN.run(merged, params)[0]["string_tag_plan"])
    assert child_metadata["label_moves"] == []
    assert len(owned(child)) == len(labels)
    assert NOTE.replace(b"\n", newline) in child
    assert not ({value(r, 5) for r in owned(child)} & {value(r, 5) for r in text_records})


@pytest.mark.parametrize("anchors,old_names,new_names,old_xy,new_xy", [
    ([(1, 1), (2, 1)], ["A", "B"], ["NW", "SE"],
     [[0.5, 0.5], [0.5, 3.5]], [[0.5, 0.5], [0.5, 3.5]]),
    ([(1, 1), (7, 1)], ["A", "B"], ["WEST-LONG", "NE"],
     [[0.5, 0.5], [7.3, 0.5]], [[0.5, 0.5], [7.3, 3.5]]),
    ([(-7, -2), (-2, -2)], ["WEST-LONG", "NE"], ["W", "E"],
     [[-9.7, -2.5], [-2.9, 0.5]], [[-9.7, -2.5], [-2.9, -2.5]]),
], ids=["same-cell", "growing-footprint", "shrinking-footprint"])
def test_reports_moved_labels_after_rename(equipped_graph, anchors, old_names, new_names, old_xy, new_xy):
    solved = equipped_graph
    solved["project"]["units"].update(drawing_units="m", meters_per_unit=1,
                                      source="explicit", drawing_unit_is_feet=False)
    inverter, = solved["inverters"]
    inverter["position"] = [anchors[0][0], anchors[0][1] - 1]
    circuit_rows = sorted(solved["strings"], key=lambda row: row["id"])
    assert len(circuit_rows) == 2
    for row, name, anchor in zip(circuit_rows, old_names, anchors):
        row.update(circuit_tag=name, route=[[0, 0], list(anchor), inverter["position"]])

    drawing_id = uuid4()
    source_bytes = source(6)
    intake = companion(source_bytes, solved)
    params = prepared(solved, paper_mm_per_drawing_unit=1)
    original_plan = BUILTIN.run(intake, params)[0]["string_tag_plan"]
    original, original_metadata = tags.splice_tags(source_bytes, intake, original_plan)
    reopened = json.loads(tags.annotation_intake(original, intake, original_metadata, drawing_id))
    assert original_metadata["label_moves"] == []

    edited = deepcopy(solved)
    edited["parent_rev"], edited["rev"] = solved["rev"], solved["rev"] + 1
    for row, name in zip(sorted(edited["strings"], key=lambda row: row["id"]), new_names):
        row["circuit_tag"] = name
    reopened.update(solar_design_graph=edited, solar_design_graph_sha256=digest(edited))
    renamed_plan = BUILTIN.run(reopened, prepared(edited, paper_mm_per_drawing_unit=1))[0]["string_tag_plan"]
    renamed, metadata = tags.splice_tags(original, reopened, renamed_plan)
    final_intake = json.loads(tags.annotation_intake(renamed, reopened, metadata, drawing_id))
    expected = [
        {"string_ref": row["id"],
         "before": {"text": old_name, "position": [*before, 0]},
         "after": {"text": new_name, "position": [*after, 0]}}
        for row, old_name, new_name, before, after in zip(circuit_rows, old_names, new_names, old_xy, new_xy)
        if before != after
    ]
    assert metadata["label_moves"] == expected
    assert "label_moves" not in final_intake["leaf_string_tags"]


def test_producer_registration_is_exact():
    assert writer.producer_entry(TOOL) == "builtins/solar_string_tags.py"
    assert writer.producer_entry({**TOOL, "entry": "authored.py"}) is None
    assert writer.producer_entry({**TOOL, "graph_input": True}) is None
    assert writer.producer_entry({**TOOL, "capabilities": ["drawing.write", "drawing.read"]}) is None
    assert "default" not in TOOL["params"]["properties"]["paper_mm_per_drawing_unit"]


@pytest.mark.parametrize("problem,code", [
    ("missing", "GRAPH_NOT_EMBEDDED"), ("digest", "GRAPH_DIGEST_MISMATCH"),
    ("native", "STRING_TAGS_NATIVE_PARENT_UNSUPPORTED"),
    ("params", "INVALID_STRING_TAGS_REQUEST"), ("scale", "INVALID_STRING_TAG_SCALE"),
    ("missing_scale", "INVALID_STRING_TAGS_REQUEST"),
    ("revision", "STALE_GRAPH_REV"),
])
def test_builtin_refuses_payload_free(solved, problem, code):
    intake, params = companion(source(), solved), prepared(solved)
    if problem == "missing":
        del intake["solar_design_graph"]
    elif problem == "digest":
        intake["solar_design_graph_sha256"] = "0" * 64
    elif problem == "native":
        intake["solar_physical_layout"] = {}
    elif problem == "params":
        params["graph"] = {"secret": "DO_NOT_LEAK"}
    elif problem == "scale":
        params["paper_mm_per_drawing_unit"] = True
    elif problem == "missing_scale":
        del params["paper_mm_per_drawing_unit"]
    else:
        params["expected_rev"] += 1
    with pytest.raises(GraphValidationError) as caught:
        BUILTIN.run(intake, params)
    # Existing graph revision codes remain owned by checked_graph.
    if problem == "revision":
        assert "REV" in caught.value.code
    else:
        assert caught.value.code == code
    assert "DO_NOT_LEAK" not in str(caught.value)


@pytest.mark.parametrize("problem", ["mismatch", "units", "unitless", "binary", "duplicate", "topology", "plan"])
def test_source_boundary_refusals(solved, problem):
    raw = source()
    intake = companion(raw, solved)
    plan = BUILTIN.run(intake, prepared(solved))[0]["string_tag_plan"]
    if problem == "mismatch":
        intake["texts"][0]["text"] = "forged"
    elif problem == "units":
        raw = source(6)
    elif problem == "unitless":
        raw = source(0)
        intake["solar_design_graph"]["project"]["units"]["source"] = "insunits"
        intake["solar_design_graph_sha256"] = digest(intake["solar_design_graph"])
    elif problem == "binary":
        raw = b"AutoCAD Binary DXF\r\n\x1a\x00"
    elif problem == "duplicate":
        raw = raw.replace(b"5\nA2\n", b"5\nA1\n")
        intake = companion(raw, solved)
    elif problem == "topology":
        raw = raw.replace(b"2\n*Model_Space\n", b"2\nOther_Space\n")
    else:
        plan["tags"][0]["text"] = "forged"
    with pytest.raises((writer.ProducerRefused, GraphValidationError)):
        tags.splice_tags(raw, intake, plan)


@pytest.mark.parametrize("problem", ["style", "layer", "lineweight", "plot_style", "marker", "reactor", "pointer", "foreign_app"])
def test_controlled_record_conflicts_refuse(solved, problem):
    intake = companion(source(), solved)
    plan = BUILTIN.run(intake, prepared(solved))[0]["string_tag_plan"]
    raw, metadata = tags.splice_tags(source(), intake, plan)
    if problem == "style":
        # Change only the new controlled style, keeping Standard untouched.
        index = raw.rfind(b"3\ntxt\n")
        raw = raw[:index] + raw[index:].replace(b"3\ntxt\n", b"3\nunsafe.ttf\n", 1)
    elif problem == "layer":
        raw = raw.replace(b"290\n1\n", b"290\n0\n")
    elif problem == "lineweight":
        raw = raw.replace(groups((370, -3), (390, "13")), groups((370, 25), (390, "13")))
    elif problem == "plot_style":
        raw = raw.replace(groups((370, -3), (390, "13")), groups((370, -3), (390, "F00")))
    elif problem == "marker":
        raw = raw.replace(tags.SCHEMA.encode(), b"forged.schema")
    elif problem in ("reactor", "foreign_app"):
        extra = groups((102, "{ACAD_REACTORS"), (330, "F00"), (102, "}")) if problem == "reactor" else groups((1001, "OTHER"), (1000, "external"))
        raw = raw.replace(groups((1001, tags.APP)), extra + groups((1001, tags.APP)), 1)
    else:
        target = next(iter(metadata["handles"].values()))
        pairs, _ = writer._pairs(raw)
        end = writer._section(pairs, "OBJECTS")[1]
        raw = (b"".join(p.raw for p in pairs[:end])
            + groups((0, "DICTIONARY"), (5, "F00"), (100, "AcDbDictionary"),
                     (3, "reference"), (350, target))
            + b"".join(p.raw for p in pairs[end:]))
    current = companion(raw, solved)
    with pytest.raises(writer.ProducerRefused):
        tags.splice_tags(raw, current, plan)


@pytest.mark.parametrize("reference", [None, "0", "F00", "ambiguous"])
def test_source_normal_plot_style_reference_refuses(solved, reference):
    raw = source()
    zero_layer = next(r for r in records(raw, "TABLES")
                      if r[0].value == "LAYER" and value(r, 2) == "0")
    original = b"".join(p.raw for p in zero_layer)
    changed = b"".join(p.raw for p in zero_layer if p.code != 390)
    if reference is not None:
        changed += groups((390, "13"), (390, "13")) if reference == "ambiguous" else groups((390, reference))
    raw = raw.replace(original, changed, 1)
    intake = companion(raw, solved)
    plan = BUILTIN.run(intake, prepared(solved))[0]["string_tag_plan"]
    with pytest.raises(writer.ProducerRefused):
        tags.splice_tags(raw, intake, plan)


@pytest.fixture(params=[1, 6, 0], ids=["inches", "metres", "explicit-unitless"])
def source_lane(request, monkeypatch, solved):
    # Seed full source topology BEFORE the genuine upload adoption in lane.
    raw = source(request.param)
    if request.node.name.split("[", 1)[0] == "test_normal_child_upsert_preserves_customer_text_on_reused_layer":
        # Only this positive reuse case gets a safe scratch layer. The exact
        # producer source retains its material pointer in every other test.
        pairs, _ = writer._pairs(raw)
        layers = [(a, b) for a, b in writer._records(pairs, writer._section(pairs, "TABLES"))
                  if pairs[a].value == "LAYER" and value(pairs[a:b], 2) == "Notes"]
        assert len(layers) == 1
        start, end = layers[0]
        assert [p.value for p in pairs[start:end] if p.code == 347] == ["21"]
        raw = b"".join(p.raw for i, p in enumerate(pairs)
                       if not (start <= i < end and p.code == 347))
    monkeypatch.setattr(editor_cases, "drawing", lambda *a, **kw: raw)
    current = request.getfixturevalue("lane")
    graph = deepcopy(solved)
    graph["project"]["units"].update(drawing_units="in" if request.param == 1 else "m",
        meters_per_unit=.0254 if request.param == 1 else 1, source="explicit", drawing_unit_is_feet=False)
    cap = checkout(current)
    # Existing server-only publication seam, with real parsing and publication;
    # no forged managed-producer state or substitute graph store.
    intake = companion(raw, graph)
    receipt = editor.save_dxf(current.tenant, current.project, current.drawing, current.parent.version_id,
        raw, request_id=uuid4(), capability=cap,
        _restored_intake=json.dumps(intake, sort_keys=True, separators=(",", ":")).encode())
    current.initial_version = current.parent.version_id
    current.parent = SimpleNamespace(version_id=UUID(receipt["drawing_version"]["version_id"]))
    current.graph, current.cap = graph, cap
    return current


@pytest.fixture
def run(source_lane, monkeypatch):
    yield from writer_cases.run.__wrapped__(source_lane, monkeypatch)


def test_normal_run_export_graph_fidelity_and_replay(source_lane, run):
    s = source_lane
    params, key = prepared(s.graph), str(uuid4())
    assert deps.find_tool(TOOL["name"], str(s.tenant))["entry"] == TOOL["entry"]
    before = context.resolve_context(s.tenant, s.project, s.parent.version_id, drawing_id=s.drawing)
    count = len(versions(s))
    response = run(TOOL, params=params, cap=s.cap, key=key)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["label_moves"] == []
    assert body["result"]["moved_label_count"] == 0
    child = body["version"]
    assert len(versions(s)) == count + 1 and child["seq"] == 3
    assert body["producer"]["source_mutation"] == "string-tag-splice"
    exported = s.http.get(s.base + f"/versions/{child['version_id']}/dxf")
    assert exported.status_code == 200
    assert exported.headers["x-leaf-source-sha256"] == hashlib.sha256(exported.content).hexdigest()
    assert body["producer"]["output_source_sha256"] == exported.headers["x-leaf-source-sha256"]
    assert len(owned(exported.content)) == len(s.graph["strings"])
    after = context.resolve_context(s.tenant, s.project, UUID(child["version_id"]), drawing_id=s.drawing)
    assert after.intake["solar_design_graph"] == before.intake["solar_design_graph"]
    assert after.intake["solar_design_graph_sha256"] == before.intake["solar_design_graph_sha256"]
    assert after.intake["project_material"] == before.intake["project_material"]
    assert "label_moves" not in after.intake["leaf_string_tags"]
    assert body["producer"]["output_intake_sha256"] == after.intake_sha256
    assert s.http.get(s.base + f"/versions/{s.parent.version_id}/dxf").content == s.raw
    assert body["producer"]["input_intake_sha256"] == before.intake_sha256
    assert body["producer"]["input_source_sha256"] == hashlib.sha256(s.raw).hexdigest()
    replay = run(TOOL, params=params, cap=s.cap, key=key)
    assert replay.status_code == 200 and replay.json()["version"] == child
    assert replay.json()["result"]["label_moves"] == body["result"]["label_moves"]
    assert run(TOOL, params={**params, "paper_height_mm": 2.1}, cap=s.cap, key=key).status_code == 409
    # Even a newly explicit default, producing identical CAD, changes the
    # confirmation and must be fenced by the internally bound params digest.
    assert run(TOOL, params={**params, "paper_height_mm": 2.0}, cap=s.cap, key=key).status_code == 409
    assert run(TOOL, params=params, cap=s.cap).status_code == 409  # stale parent
    assert len(versions(s)) == count + 1
    released = s.http.delete(s.base + "/checkout", headers={"X-Checkout-Capability": s.cap})
    assert released.status_code == 200, released.text
    replay = run(TOOL, params=params, cap=s.cap, key=key)
    assert replay.status_code == 200 and replay.json()["version"] == child
    assert len(versions(s)) == count + 1


def test_normal_child_upsert_preserves_customer_text_on_reused_layer(source_lane, run):
    s = source_lane
    expected_note = next(b"".join(p.raw for p in r) for r in records(s.raw) if value(r, 5) == "A1")
    expected_line = next(b"".join(p.raw for p in r) for r in records(s.raw) if value(r, 5) == "A2")
    notes_layer = next(r for r in records(s.raw, "TABLES")
                       if r[0].value == "LAYER" and value(r, 2) == "Notes")
    assert not any(p.code == 347 for p in notes_layer)
    expected_layer = b"".join(p.raw for p in notes_layer)
    count = len(versions(s))
    first = run(TOOL, params=prepared(s.graph), cap=s.cap)
    assert first.status_code == 200, first.text
    child = first.json()["version"]["version_id"]
    second = run(TOOL, version=child, params=prepared(s.graph, tag_layer="Notes"), cap=s.cap)
    assert second.status_code == 200, second.text
    newer = second.json()["version"]["version_id"]
    output = s.http.get(s.base + f"/versions/{newer}/dxf")
    assert output.status_code == 200
    assert all(kept in output.content for kept in (expected_note, expected_line, expected_layer))
    for section in ("TABLES", "BLOCKS", "ENTITIES", "OBJECTS"):
        for rec in records(s.raw, section):
            if rec[0].value == "TABLE" and value(rec, 2) in ("LAYER", "STYLE", "APPID"):
                continue  # Only the required table counts may change.
            assert b"".join(p.raw for p in rec) in output.content
    parent = s.http.get(s.base + f"/versions/{s.parent.version_id}/dxf")
    assert parent.status_code == 200 and parent.content == s.raw
    assert output.headers["x-leaf-source-sha256"] == hashlib.sha256(output.content).hexdigest()
    assert len(owned(output.content)) == len(s.graph["strings"])
    assert {tuple(p.value for p in rec if p.code == 1000) for rec in owned(output.content)} == {
        (tags.SCHEMA, string["id"]) for string in s.graph["strings"]}
    assert all(value(rec, 8) == "Notes" for rec in owned(output.content))
    stored = context.resolve_context(s.tenant, s.project, UUID(newer), drawing_id=s.drawing)
    assert stored.intake["solar_design_graph"] == s.graph
    assert stored.intake["solar_design_graph_sha256"] == digest(s.graph)
    assert len(versions(s)) == count + 2


def test_normal_reused_layer_material_pointer_refuses_without_version(source_lane, run):
    s = source_lane
    assert s.raw in (source(1), source(6), source(0))
    notes_layer = next(r for r in records(s.raw, "TABLES")
                       if r[0].value == "LAYER" and value(r, 2) == "Notes")
    assert [p.value for p in notes_layer if p.code == 347] == ["21"]
    assert b"".join(p.raw for p in notes_layer) == LAYER
    count = len(versions(s))
    response = run(TOOL, params=prepared(s.graph, tag_layer="Notes"), cap=s.cap)
    assert response.status_code == 422, response.text
    assert response.json()["reason_code"] == "STRING_TAGS_LAYER_CONFLICT"
    assert len(versions(s)) == count
    parent = s.http.get(s.base + f"/versions/{s.parent.version_id}/dxf")
    assert parent.status_code == 200 and parent.content == s.raw


def test_normal_run_admission_creates_no_versions(source_lane, run):
    s = source_lane
    params, count = prepared(s.graph), len(versions(s))
    assert run(TOOL, params=params).status_code == 403
    assert run(TOOL, params=params, cap="lco1." + "0" * 64).status_code == 403
    assert run(TOOL, params=params, cap=s.cap, org=uuid4()).status_code in (403, 409)
    assert run(TOOL, params=params, cap=s.cap, version=uuid4()).status_code in (404, 409)
    assert run(TOOL, params=params, cap=s.cap, digest=False).status_code == 409
    invalid = run(TOOL, params={**params, "graph": {}}, cap=s.cap)
    assert invalid.status_code in (400, 422), invalid.text
    assert len(versions(s)) == count


def test_normal_run_missing_companion_and_native_refuse(source_lane, run):
    s = source_lane
    params, count = prepared(s.graph), len(versions(s))
    missing = run(TOOL, params=params, cap=s.cap, version=s.initial_version)
    assert missing.status_code == 422, missing.text
    assert missing.json()["reason_code"] == "GRAPH_NOT_EMBEDDED"
    intake = companion(s.raw, s.graph)
    intake["solar_physical_layout"] = {"untrusted": "DO_NOT_LEAK"}
    receipt = editor.save_dxf(s.tenant, s.project, s.drawing, s.parent.version_id, s.raw,
        request_id=uuid4(), capability=s.cap,
        _restored_intake=json.dumps(intake, sort_keys=True, separators=(",", ":")).encode())
    native = run(TOOL, params=params, cap=s.cap, version=receipt["drawing_version"]["version_id"])
    assert native.status_code == 422, native.text
    assert native.json()["reason_code"] == "STRING_TAGS_NATIVE_PARENT_UNSUPPORTED"
    assert "DO_NOT_LEAK" not in native.text
    assert len(versions(s)) == count + 1  # only the explicit server-side fixture seed
